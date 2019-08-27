"""PS Synchronizer."""
from functools import partial
from tensorflow.python import ops
from tensorflow.python.framework import device_spec, dtypes, constant_op
from tensorflow.python.ops import math_ops, data_flow_ops, gen_control_flow_ops, \
    control_flow_ops
from tensorflow.python.platform import tf_logging as logging

from autodist.const import MAX_INT64, UPDATE_OP_VAR_POS, AUTODIST_PREFIX
from autodist.kernel.common import utils, resource_variable
from autodist.kernel.common.utils import get_op_name, get_consumers, get_ancestors, traverse, update_consumers, \
    update_control_consumers, replica_prefix
from autodist.kernel.synchronization.synchronizer import Synchronizer


class PSSynchronizer(Synchronizer):
    """PS Synchronizer."""

    def __init__(
            self,
            reduction_destinations=None,
            local_replication=True,
            sync=True
    ):
        # TODO: partitions
        self.target_device = reduction_destinations[0] if reduction_destinations else ""
        self._local_replication = local_replication
        self._sync = sync

        self._var_op_to_agg_grad = {}
        self._var_op_to_accum_apply_op = {}
        super().__init__()

    def in_graph_apply(self, old_graph_item, curr_graph_item, grad, target):
        """
        Apply in-graph synchronization to the grad and target in the graph.

        Args:
            old_graph_item (GraphItem): The old, un-synchronized graph.
            curr_graph_item (GraphItem): The graph to put the new ops in.
            grad: The gradient object.
            target: The target tensor.
            num_replicas: The number of replicas to create.

        Returns:
            GraphItem
        """
        # Hierarchical reduction at local node
        reduce_to_device = '/device:CPU:0'
        assert target.op.name in [var.op.name for var in ops.get_collection(ops.GraphKeys.TRAINABLE_VARIABLES)]

        # Aggregate grad
        if isinstance(grad, ops.Tensor):
            # Dense gradient
            values_name = grad.name
            agg_grad = self._get_aggregated_dense_grad(curr_graph_item, values_name, reduce_to_device)

            # Make gradients consumers to consume the aggregated gradients.
            self._update_gradient_consumers(curr_graph_item,
                                            self._get_ops_in_new_graph(curr_graph_item, grad.consumers()),
                                            self._get_ops_in_new_graph(curr_graph_item,
                                                                       old_graph_item.control_consumers[grad.op]),
                                            values_name,
                                            agg_grad)
        elif isinstance(grad, ops.IndexedSlices):
            # Sparse gradient
            agg_grad = self._get_aggregated_sparse_grad(target.op, grad, reduce_to_device)

            indices_cc_ops = self._get_ops_in_new_graph(curr_graph_item,
                                                        old_graph_item.control_consumers[grad.indices.op])
            indices_c_ops = self._get_ops_in_new_graph(curr_graph_item, grad.indices.consumers())
            values_cc_ops = self._get_ops_in_new_graph(curr_graph_item,
                                                       old_graph_item.control_consumers[grad.values.op])
            values_c_ops = self._get_ops_in_new_graph(curr_graph_item, grad.values.consumers())

            self._update_gradient_consumers(curr_graph_item,
                                            indices_c_ops,
                                            indices_cc_ops,
                                            grad.indices.name,
                                            agg_grad.indices)
            self._update_gradient_consumers(curr_graph_item,
                                            values_c_ops,
                                            list(set(values_cc_ops).difference(indices_cc_ops)),
                                            grad.values.name,
                                            agg_grad.values)
        else:
            raise RuntimeError("Incorrect grad.")

    # pylint: disable=arguments-differ
    def between_graph_apply(self, graph_item, update_op, gradient, target):
        """
        Apply between-graph synchronization to the target ops in the graph.

        Args:
            graph_item: The current graph.
            update_op: The update op.
            gradient: The target gradient.
            target: The target op.

        Returns:
            ResourceVariableReplicator
        """
        variable_replicator = None
        if self._local_replication:
            variable_replicator = self._replicate_variable_to_devices(
                graph_item, gradient, target, self.worker_device, self.num_replicas
            )

        self._var_op_to_agg_grad, self._var_op_to_accum_apply_op = self._get_accumulation_ops(graph_item, gradient,
                                                                                              target, self.num_workers)

        if self._sync:
            self.add_sync_op(graph_item, update_op, variable_replicator)

        return variable_replicator

    def add_sync_op(self, graph_item, var_update_op, variable_replicator=None):
        """
        Adds additional ops needed for synchronous distributed training into current graph.

        Main purpose of additional ops are:
        1. Initialization
        2. Synchronization
        3. Gradient aggregation

        Args:
            graph_item (GraphItem): the graph
            var_update_op: The op
            variable_replicator: The dictionary of master variable op name
                -> list of replicated variables, could be None

        Returns:
            None
        """
        this_worker_cpu = device_spec.DeviceSpecV2.from_string(self.worker_device)
        this_worker_cpu = this_worker_cpu.replace(device_type='CPU', device_index=0)
        is_chief = self.worker_id == 0

        global_step_op = ops.get_collection(ops.GraphKeys.GLOBAL_STEP)[0].op \
            if ops.get_collection(ops.GraphKeys.GLOBAL_STEP) else None

        var_op = var_update_op.inputs[UPDATE_OP_VAR_POS].op
        is_trainable = var_op in graph_item.trainable_vars

        with ops.device(var_op.device), ops.name_scope(""):
            queue_ops = self._get_queue_ops(var_update_op, is_chief, is_trainable)

            # Only dense trainable variables are replicated locally
            if variable_replicator:
                mirror_variable_update_ops = variable_replicator.get_all_update_ops(
                    queue_ops,
                    worker_device=this_worker_cpu
                )
                with ops.device(this_worker_cpu):
                    finish_op = control_flow_ops.group(*mirror_variable_update_ops)
            else:
                finish_op = control_flow_ops.group(*queue_ops)

            # Exceptional case: add additional dependencies for global_step
            if ops.get_collection(ops.GraphKeys.GLOBAL_STEP):
                assert len(ops.get_collection(ops.GraphKeys.GLOBAL_STEP)) == 1
                if var_op == global_step_op and not is_chief:
                    # Chief worker's finish_op already has update_op as control input
                    deps = [finish_op]
                    deps.extend([inp.op for inp in var_update_op.inputs])
                    deps.extend([inp for inp in var_update_op.control_inputs])
                    finish_op = control_flow_ops.group(*deps)

        # Place computation ops of aggregated gradients on PS
        # Note that even though this is doing a graph traversal, it is called in such a way that it
        # only traverses from a gradient aggregator op to a gradient application op (or vice versa) --
        # these corresponding ops should always be adjacent in the graph.
        self._place_post_grad_agg_ops(device_spec.DeviceSpecV2.from_string(self.target_device),
                                      self._var_op_to_agg_grad, {var_op: var_update_op} if is_trainable else {})

        # Replace variable update op with finish_op (control input)
        # or read_op (input)
        update_control_consumers(graph_item.control_consumers[var_update_op], var_update_op, finish_op)

    # pylint: disable=too-many-branches
    def _get_queue_ops(self, var_update_op, is_chief, is_trainable):
        var_op = var_update_op.inputs[UPDATE_OP_VAR_POS].op

        var_update_sync_queues = \
            [data_flow_ops.FIFOQueue(1, [dtypes.bool], shapes=[[]],
                                     name='auto_parallel_%s_update_sync_queue_%d' % (var_op.name, i),
                                     shared_name='auto_parallel_%s_update_sync_queue_%d' % (var_op.name, i))
             for i in range(self.num_workers)]

        queue_ops = []
        if is_chief:
            if is_trainable:
                var_update_deps = [self._var_op_to_accum_apply_op[var_op], var_update_op]
            else:
                var_update_deps = [var_update_op]
            # Chief enqueues tokens to all other workers
            # after executing variable update
            token = constant_op.constant(False)
            with ops.control_dependencies(var_update_deps):
                for i, q in enumerate(var_update_sync_queues):
                    if i != self.worker_id:
                        queue_ops.append(q.enqueue(token))
                    else:
                        queue_ops.append(gen_control_flow_ops.no_op())
        else:
            # wait for execution of var_update_op
            if is_trainable:
                with ops.control_dependencies([self._var_op_to_accum_apply_op[var_op]]):
                    dequeue = var_update_sync_queues[self.worker_id].dequeue()
            else:
                dequeue = var_update_sync_queues[self.worker_id].dequeue()
            queue_ops.append(dequeue)
        return queue_ops

    def _get_aggregated_dense_grad(self, graph_item, grad_name, reduce_to_device):
        grad_op_name = get_op_name(grad_name)
        output_idx = int(grad_name.split(':')[1])
        grad_ops = [
            graph_item.graph.get_operation_by_name(ops.prepend_name_scope(grad_op_name, replica_prefix(i)))
            for i in range(self.num_replicas)
        ]

        # Aggregate gradients on `reduce_to_device` (usually CPU)
        with ops.device(reduce_to_device):
            grad_sum_op_name = ops.prepend_name_scope(grad_op_name, u"%sAdd" % AUTODIST_PREFIX)
            grad_sum = math_ops.add_n([grad_op.outputs[output_idx] for grad_op in grad_ops], name=grad_sum_op_name)
            grad_avg_op_name = ops.prepend_name_scope(grad_op_name, u"%sDiv" % AUTODIST_PREFIX)
            grad_avg = math_ops.realdiv(grad_sum, self.num_replicas, name=grad_avg_op_name)
        return grad_avg

    def _get_aggregated_sparse_grad(self, var_op, grad, reduce_to_device):
        indices_op_name = get_op_name(grad.indices.name)
        values_op_name = get_op_name(grad.values.name)
        dense_shape_op_name = get_op_name(grad.dense_shape.name)

        indexed_slices_grads = []
        for i in range(self.num_replicas):
            indices_op = ops.get_default_graph().get_operation_by_name(
                ops.prepend_name_scope(indices_op_name, replica_prefix(i)))
            values_op = ops.get_default_graph().get_operation_by_name(
                ops.prepend_name_scope(values_op_name, replica_prefix(i)))
            dense_shape_op = ops.get_default_graph().get_operation_by_name(
                ops.prepend_name_scope(dense_shape_op_name, replica_prefix(i)))
            indexed_slices_grads.append(
                ops.IndexedSlices(
                    values_op.outputs[utils.get_index_from_tensor_name(grad.values.name)],
                    indices_op.outputs[utils.get_index_from_tensor_name(grad.indices.name)],
                    dense_shape_op.outputs[utils.get_index_from_tensor_name(grad.dense_shape.name)])
            )

        def _aggregate_gradients():
            with ops.device(reduce_to_device):
                grad_accum_op_name = \
                    ops.prepend_name_scope(values_op_name,
                                           u"%sAccum" % AUTODIST_PREFIX)
                grad_accum = data_flow_ops.SparseConditionalAccumulator(
                    dtype=indexed_slices_grads[0].values.dtype,
                    shape=var_op.outputs[0].shape,
                    shared_name=grad_accum_op_name,
                    name=grad_accum_op_name)
                accum_apply_ops = [grad_accum.apply_indexed_slices_grad(
                    indexed_slices_grads[i],
                    MAX_INT64,
                    name=ops.prepend_name_scope(
                        values_op_name,
                        u"%s-Accum-Apply" % replica_prefix(i)))
                    for i in range(self.num_replicas)]
                take_grad_op_name = ops.prepend_name_scope(values_op_name, u"%sTake-Grad" % AUTODIST_PREFIX)
                with ops.control_dependencies(accum_apply_ops):
                    take_grad = grad_accum.take_indexed_slices_grad(self.num_replicas, name=take_grad_op_name)

                new_indices = take_grad.indices
                new_values = take_grad.values
                new_dense_shape = take_grad.dense_shape
                if indexed_slices_grads[0].indices.dtype != new_indices.dtype:
                    new_indices = math_ops.cast(
                        new_indices,
                        indexed_slices_grads[0].indices.dtype,
                        name=ops.prepend_name_scope(
                            values_op_name,
                            u"%sTake-Grad-Cast-Indices" % AUTODIST_PREFIX)
                    )
                if indexed_slices_grads[0].dense_shape.dtype != new_dense_shape.dtype:
                    new_dense_shape = math_ops.cast(
                        new_dense_shape,
                        indexed_slices_grads[0].dense_shape.dtype,
                        name=ops.prepend_name_scope(
                            values_op_name,
                            u"%sTake-Grad-Cast-Shape" % AUTODIST_PREFIX)
                    )
            return ops.IndexedSlices(new_values, new_indices, new_dense_shape)

        return _aggregate_gradients()

    @staticmethod
    def _replicate_variable_to_devices(graph_item, gradient, target, worker_device, num_replicas_per_worker):

        if not isinstance(gradient, ops.Tensor):
            # Do not replicate sparse variables
            return None

        worker_device = device_spec.DeviceSpecV2.from_string(worker_device)
        master_var = graph_item.trainable_var_op_to_var.get(target.op)
        mirror_var_device = device_spec.DeviceSpecV2(job=worker_device.job,
                                                     replica=worker_device.replica,
                                                     task=worker_device.task)
        resource_var_replicator = resource_variable.ResourceVariableReplicator(
            master_var
        ).build_mirror_vars(mirror_var_device, num_replicas_per_worker)

        return resource_var_replicator

    @staticmethod
    def _get_accumulation_ops(graph_item, gradient, target, num_workers):
        def _get_accum_apply_and_agg_grad(var_op, grad, indices, dense_shape):
            if indices is None:
                tensor = resource_variable.get_read_var_tensor(var_op)
                grad_accum = data_flow_ops.ConditionalAccumulator(
                    grad.dtype,
                    shape=tensor.get_shape(),
                    shared_name=var_op.name + "/grad_accum")
                # Get a copy of consumers list before creating accum_apply_op
                grad_consumers = [c for c in grad.consumers()]
                accum_apply_op = grad_accum.apply_grad(
                    grad, local_step=MAX_INT64,
                    name=grad.op.name + '_accum_apply_grad')
                agg_grad = grad_accum.take_grad(num_workers,
                                                name=var_op.name + '_take_grad')
                update_consumers(grad_consumers, grad, agg_grad)
                update_control_consumers(graph_item.control_consumers[grad.op],
                                         grad.op, agg_grad.op)
            else:
                grad_indexed_slices = ops.IndexedSlices(values=grad, indices=indices,
                                                        dense_shape=dense_shape)
                grad_accum = data_flow_ops.SparseConditionalAccumulator(
                    grad.dtype,
                    shape=grad.shape,
                    shared_name=var_op.name + "/grad_accum")
                # Get a copy of consumers list before creating accum_apply_op
                indices_consumers = [c for c in indices.consumers()]
                grad_consumers = [c for c in grad.consumers()]
                accum_apply_op = grad_accum.apply_indexed_slices_grad(
                    grad_indexed_slices, local_step=MAX_INT64,
                    name=grad.op.name + '_accum_apply_grad')
                agg_grad = grad_accum.take_indexed_slices_grad(
                    num_workers, name=var_op.name + '_take_grad')
                agg_indices = agg_grad.indices
                if indices.dtype != agg_grad.indices.dtype:
                    agg_indices = math_ops.cast(agg_grad.indices, indices.dtype)
                agg_grad = ops.IndexedSlices(values=agg_grad.values,
                                             indices=agg_indices,
                                             dense_shape=agg_grad.dense_shape)
                assert isinstance(agg_grad, ops.IndexedSlices)
                update_consumers(indices_consumers, indices, agg_grad.indices)
                update_consumers(grad_consumers, grad, agg_grad.values)
                update_control_consumers(graph_item.control_consumers[indices.op],
                                         indices.op, agg_grad.indices.op)
                update_control_consumers(graph_item.control_consumers[grad.op],
                                         grad.op, agg_grad.values.op)
            return accum_apply_op, agg_grad

        # Aggregate gradients from different workers using ConditionalAccumulator.
        # var_op_to_agg_grad and var_op_to_accum_apply_op are updated.
        var_op_to_agg_grad = {}
        var_op_to_accum_apply_op = {}

        trainable_var_op_to_var = {var.op: var for var in ops.get_collection(ops.GraphKeys.TRAINABLE_VARIABLES)}
        if target.op not in trainable_var_op_to_var:
            logging.debug(
                "Gradient for non-trainable variable %s is created, "
                "do not insert accumulator for aggregating this gradient"
                % target.op.name)
            return {}, {}

        var_op = target.op
        if isinstance(gradient, ops.Tensor):
            grad = gradient
            indices = None
            dense_shape = None
        else:
            grad = gradient.values
            indices = gradient.indices
            dense_shape = gradient.dense_shape
        with ops.device(var_op.device), ops.name_scope(""):
            accum_apply_op, agg_grad = _get_accum_apply_and_agg_grad(var_op, grad, indices, dense_shape)
        if indices is None:
            var_op_to_agg_grad[var_op] = (None, agg_grad)
        else:
            var_op_to_agg_grad[var_op] = (agg_grad.indices, agg_grad.values)
        var_op_to_accum_apply_op[var_op] = accum_apply_op
        return var_op_to_agg_grad, var_op_to_accum_apply_op

    @staticmethod
    def _place_post_grad_agg_ops(ps_device, var_op_to_agg_grad, var_op_to_apply_grad_op):
        op_to_task = {}
        agg_grad_ops = []
        for var_op, agg_grad in var_op_to_agg_grad.items():
            var_device = device_spec.DeviceSpecV2.from_string(var_op.device)
            if agg_grad[0] is not None:
                agg_grad_ops.append(agg_grad[0].op)
                op_to_task[agg_grad[0].op] = var_device.task
            agg_grad_ops.append(agg_grad[1].op)
            op_to_task[agg_grad[1].op] = var_device.task

        apply_grad_ops = []
        for var_op, apply_grad_op in var_op_to_apply_grad_op.items():
            var_device = device_spec.DeviceSpecV1.from_string(var_op.device)
            apply_grad_ops.append(apply_grad_op)
            # colocate apply_grad and variable
            apply_grad_op._set_device(var_device)
            op_to_task[apply_grad_op] = var_device.task

        # Make sure that the agg_grad_ops and apply_grad_ops are assigned the same task, if possible
        PSGradientTaskAssigner(op_to_task, agg_grad_ops, apply_grad_ops, ps_device).assign()


class PSGradientTaskAssigner:
    """Make sure that all corresponding PS gradient ops are assigned to the same task."""

    SHARED_TASK_ID = -1  # Default value to use when marking a task as shared across devices

    def __init__(self, op_to_task, agg_grad_ops, apply_grad_ops, ps_device):
        self._op_to_task = op_to_task
        self._agg_grad_ops = agg_grad_ops
        self._apply_grad_ops = apply_grad_ops
        self._ps_device = ps_device

        # Note(gyeongin): Need to include control dependency ops in ancestors and
        # descendants or not?
        self._apply_grad_ancestor_ops = get_ancestors(self._apply_grad_ops, self._agg_grad_ops)
        self._agg_grad_descendant_ops = traverse(self._agg_grad_ops, end_ops=self._apply_grad_ops)
        self._ancestors_diff_descendants = self._apply_grad_ancestor_ops.difference(self._agg_grad_descendant_ops)

        logging.debug(f"apply_grad_ancestor_ops: {len(self._apply_grad_ancestor_ops)}")
        logging.debug(f"agg_grad_descendant_ops: {len(self._agg_grad_descendant_ops)}")
        logging.debug(f"ancestors diff descendants: {len(self._ancestors_diff_descendants)}")

    def assign(self):
        """Bi-directionally traverse the graph and assign tasks to ops."""
        # Parent-to-child traversal
        fn = partial(self.__assign_forward, end_ops=self._apply_grad_ops)
        traverse(self._agg_grad_ops, end_ops=self._apply_grad_ops, neighbors_fn=fn)
        # Child-to-parent traversal
        fn = partial(self.__assign_backward, end_ops=self._agg_grad_ops)
        traverse(self._apply_grad_ops, self._agg_grad_ops, neighbors_fn=fn)

    def __assign_forward(self, curr_op, end_ops=None):
        """Get children of and assign a task for `curr_op`. To be used as the `neighbors_fn` for `traverse`."""
        end_ops = end_ops or set()
        if curr_op in self._op_to_task and curr_op not in end_ops:
            return [consumer for consumer in get_consumers(curr_op) if consumer in self._apply_grad_ancestor_ops]

        placement_reference_ops = {input_tensor.op for input_tensor in curr_op.inputs}.\
            difference(self._ancestors_diff_descendants)

        if not all(ref_op in self._op_to_task for ref_op in placement_reference_ops):
            # At least one of `placement_reference_ops` doesn't have a task assigned yet,
            # so re-add `curr_op` to the queue and wait for them to all have tasks
            return [curr_op]

        self.__assign_task(curr_op, placement_reference_ops)

        if curr_op not in end_ops:
            return [consumer for consumer in get_consumers(curr_op) if consumer in self._apply_grad_ancestor_ops]

        return []

    def __assign_backward(self, curr_op, end_ops=None):
        """Get parents of and assign a task for `curr_op`. To be used as the `neighbors_fn` for `traverse`."""
        end_ops = end_ops or set()
        if curr_op in self._op_to_task and curr_op not in end_ops:
            return [input_tensor.op for input_tensor in curr_op.inputs]

        placement_reference_ops = set(get_consumers(curr_op)).intersection(self._apply_grad_ancestor_ops)

        if not all(ref_op in self._op_to_task for ref_op in placement_reference_ops):
            # At least one of `placement_reference_ops` doesn't have a task assigned yet,
            # so re-add `curr_op` to the queue and wait for them to all have tasks
            return [curr_op]

        self.__assign_task(curr_op, placement_reference_ops)

        if curr_op not in end_ops:
            return [input_tensor.op for input_tensor in curr_op.inputs]

        return []

    def __assign_task(self, curr_op, placement_reference_ops):
        """Given an op, assign it a task based on the task assignments of its reference ops."""
        placement_reference_tasks = [self._op_to_task[ref_op] for ref_op in placement_reference_ops]
        unique_tasks = set(placement_reference_tasks)

        if not unique_tasks:
            raise RuntimeError(f"Should have placement reference for operation {curr_op.name}")
        elif len(unique_tasks) == 1:
            curr_op_task = unique_tasks.pop()
            self._op_to_task[curr_op] = curr_op_task
        else:
            # priority: assigned placement > shared
            if self.SHARED_TASK_ID in unique_tasks:
                unique_tasks.remove(self.SHARED_TASK_ID)
            if len(unique_tasks) == 1:
                curr_op_task = unique_tasks.pop()
                self._op_to_task[curr_op] = curr_op_task
            else:
                # multiple device placement -> shared
                assert len(unique_tasks) > 1
                curr_op_task = self.SHARED_TASK_ID
                self._op_to_task[curr_op] = self.SHARED_TASK_ID

        logging.debug(f"post_grad_agg_op {curr_op.name} is assigned to ps task {curr_op_task}")
        if curr_op_task == self.SHARED_TASK_ID:
            # TODO: do not assign all shared ops to task 0
            # - we can do better
            curr_op_task = 0

        ps_device = self._ps_device.replace(task=curr_op_task)
        curr_op._set_device(ps_device)