from logging import getLogger
from time import sleep

import numpy as np
from openfl.component.collaborator import Collaborator
from openfl.component.collaborator.collaborator import DevicePolicy
from openfl.component.collaborator.collaborator import OptTreatment
from openfl.pipelines import NoCompressionPipeline
from openfl.protocols import utils
from openfl.utilities import TensorKey

from unito.openfl_ext.tensor_codec import TensorCodec
from unito.openfl_ext.tensor_db import TensorDB


class Collaborator(Collaborator):
    r"""The Collaborator object class.

        Args:
            collaborator_name (string): The common name for the collaborator
            aggregator_uuid: The unique id for the client
            federation_uuid: The unique id for the federation
            model: The model
            opt_treatment* (string): The optimizer state treatment (Defaults to
                "CONTINUE_GLOBAL", which is aggreagated state from previous round.)

            compression_pipeline: The compression pipeline (Defaults to None)

            num_batches_per_round (int): Number of batches per round
                                         (Defaults to None)

            delta_updates* (bool): True = Only model delta gets sent.
                                   False = Whole model gets sent to collaborator.
                                   Defaults to False.

            single_col_cert_common_name: (Defaults to None)

        Note:
            \* - Plan setting.
        """

    def __init__(self,
                 collaborator_name,
                 aggregator_uuid,
                 federation_uuid,
                 client,
                 task_runner,
                 task_config,
                 opt_treatment='RESET',
                 device_assignment_policy='CPU_ONLY',
                 delta_updates=False,
                 compression_pipeline=None,
                 db_store_rounds=1,
                 nn=False,
                 **kwargs):
        """Initialize."""
        self.single_col_cert_common_name = None

        self.nn = nn

        if self.single_col_cert_common_name is None:
            self.single_col_cert_common_name = ''  # for protobuf compatibility
        # we would really want this as an object

        self.collaborator_name = collaborator_name
        self.aggregator_uuid = aggregator_uuid
        self.federation_uuid = federation_uuid

        self.compression_pipeline = compression_pipeline or NoCompressionPipeline()
        self.tensor_codec = TensorCodec(self.compression_pipeline)
        self.tensor_db = TensorDB(self.nn)
        self.db_store_rounds = 1  # db_store_rounds

        self.task_runner = task_runner
        self.delta_updates = delta_updates

        self.client = client

        self.task_config = task_config

        self.logger = getLogger(__name__)

        # @TODO: AdaBoost variables
        self.adaboost_coeff = [1 / self.task_runner.get_train_data_size()] * self.task_runner.get_train_data_size()
        self.model_buffer = None
        self.errors = []

        # RESET/CONTINUE_LOCAL/CONTINUE_GLOBAL
        if hasattr(OptTreatment, opt_treatment):
            self.opt_treatment = OptTreatment[opt_treatment]
        else:
            self.logger.error(f'Unknown opt_treatment: {opt_treatment.name}.')
            raise NotImplementedError(f'Unknown opt_treatment: {opt_treatment}.')

        if hasattr(DevicePolicy, device_assignment_policy):
            self.device_assignment_policy = DevicePolicy[device_assignment_policy]
        else:
            self.logger.error('Unknown device_assignment_policy: '
                              f'{device_assignment_policy.name}.')
            raise NotImplementedError(
                f'Unknown device_assignment_policy: {device_assignment_policy}.'
            )

        self.task_runner.set_optimizer_treatment(self.opt_treatment.name)

    def run(self):
        """Run the collaborator."""
        while True:
            tasks, round_number, sleep_time, time_to_quit = self.get_tasks()
            if time_to_quit:
                break
            elif sleep_time > 0:
                sleep(sleep_time)  # some sleep function
            else:
                self.logger.info(f'Received the following tasks: {tasks}')
                for task in tasks:
                    self.do_task(task, round_number)

                # Cleaning tensor db
                self.tensor_db.clean_up(self.db_store_rounds + 1)

        self.logger.info('End of Federation reached. Exiting...')

    def run_simulation(self):
        """
        Specific function for the simulation.

        After the tasks have
        been performed for a roundquit, and then the collaborator object will
        be reinitialized after the next round
        """
        while True:
            tasks, round_number, sleep_time, time_to_quit = self.get_tasks()
            if time_to_quit:
                self.logger.info('End of Federation reached. Exiting...')
                break
            elif sleep_time > 0:
                sleep(sleep_time)  # some sleep function
            else:
                self.logger.info(f'Received the following tasks: {tasks}')
                for task in tasks:
                    self.do_task(task, round_number)
                self.logger.info(f'All tasks completed on {self.collaborator_name} '
                                 f'for round {round_number}...')
                break

    def do_task(self, task, round_number):
        """Do the specified task."""
        # map this task to an actual function name and kwargs
        func_name = self.task_config[task]['function']
        kwargs = self.task_config[task]['kwargs']

        # this would return a list of what tensors we require as TensorKeys
        # @TODO: nn should be passed from above
        required_tensorkeys_relative = self.task_runner.get_required_tensorkeys_for_function(
            func_name,
            nn=False,
            **kwargs
        )

        # models actually return "relative" tensorkeys of (name, LOCAL|GLOBAL,
        # round_offset)
        # so we need to update these keys to their "absolute values"
        required_tensorkeys = []
        for tname, origin, rnd_num, report, tags in required_tensorkeys_relative:
            if origin == 'GLOBAL':
                origin = self.aggregator_uuid
            else:
                origin = self.collaborator_name

            # rnd_num is the relative round. So if rnd_num is -1, get the
            # tensor from the previous round
            required_tensorkeys.append(
                TensorKey(tname, origin, rnd_num + round_number, report, tags)
            )

        # print('Required tensorkeys = {}'.format(
        # [tk[0] for tk in required_tensorkeys]))
        input_tensor_dict = self.get_numpy_dict_for_tensorkeys(
            required_tensorkeys,
            **kwargs
        )

        # now we have whatever the model needs to do the task
        if hasattr(self.task_runner, 'TASK_REGISTRY'):
            # New interactive python API
            # New `Core` TaskRunner contains registry of tasks
            func = self.task_runner.TASK_REGISTRY[func_name]
            self.logger.info('Using Interactive Python API')

            # So far 'kwargs' contained parameters read from the plan
            # those are parameters that the experiment owner registered for
            # the task.
            # There is another set of parameters that created on the
            # collaborator side, for instance, local processing unit identifier:s
            if (self.device_assignment_policy is DevicePolicy.CUDA_PREFERRED
                    and len(self.cuda_devices) > 0):
                kwargs['device'] = f'cuda:{self.cuda_devices[0]}'
            else:
                kwargs['device'] = 'cpu'
        else:
            # TaskRunner subclassing API
            # Tasks are defined as methods of TaskRunner
            func = getattr(self.task_runner, func_name)
            self.logger.info('Using TaskRunner subclassing API')

        # @TODO: this is too much ad hoc
        if task == '1_train' or task == '2_weak_learners_validate':
            kwargs['adaboost_coeff'] = self.adaboost_coeff

        global_output_tensor_dict, local_output_tensor_dict, optional = func(
            col_name=self.collaborator_name,
            round_num=round_number,
            input_tensor_dict=input_tensor_dict,
            **kwargs)

        # @TODO: this is too much ad hoc
        if task == '2_weak_learners_validate':
            self.model_buffer = input_tensor_dict['generic_model']
            self.errors = optional
        if task == '3_adaboost_update':
            # @TODO assign a better name too this
            input_tensor_dict = input_tensor_dict['generic_model']
            coeff = input_tensor_dict[0]
            best_model = int(input_tensor_dict[1])

            self.adaboost_coeff = [self.adaboost_coeff[i] * np.exp(-coeff * ((-1) ** self.errors[best_model][i]))
                                   for i in range(self.task_runner.get_train_data_size())]
            self.adaboost_coeff /= sum(self.adaboost_coeff)

            adaboost = self.tensor_db.get_tensor_from_cache(TensorKey(
                'generic_model',
                self.collaborator_name,
                round_number - 1,
                False,
                ('adaboost',)
            ))

            if adaboost is not None:
                adaboost.add(self.model_buffer.get(best_model), coeff)
            else:
                adaboost = self.model_buffer.replace(self.model_buffer.get(best_model), coeff)

            self.tensor_db.cache_tensor({TensorKey(
                'generic_model',
                self.collaborator_name,
                round_number,
                False,
                ('adaboost',)
            ): adaboost})

        # Save global and local output_tensor_dicts to TensorDB
        self.tensor_db.cache_tensor(global_output_tensor_dict)
        self.tensor_db.cache_tensor(local_output_tensor_dict)

        # send the results for this tasks; delta and compression will occur in
        # this function

        self.send_task_results(global_output_tensor_dict, round_number, task, kwargs)

    def send_task_results(self, tensor_dict, round_number, task_name, kwargs):
        """Send task results to the aggregator."""
        named_tensors = [
            self.nparray_to_named_tensor(k, v) for k, v in tensor_dict.items()
        ]

        # for general tasks, there may be no notion of data size to send.
        # But that raises the question how to properly aggregate results.

        data_size = -1

        if 'data' in kwargs:
            if kwargs['data'] == 'test':
                data_size = self.task_runner.get_valid_data_size()
            else:
                data_size = self.task_runner.get_train_data_size()

        self.logger.debug(f'{task_name} data size = {data_size}')

        for tensor in tensor_dict:
            tensor_name, origin, fl_round, report, tags = tensor

            if report:
                self.logger.metric(
                    f'Round {round_number}, collaborator {self.collaborator_name} '
                    f'is sending metric for task {task_name}:'
                    f' {tensor_name}\t{tensor_dict[tensor]}')

        self.client.send_local_task_results(
            self.collaborator_name, round_number, task_name, data_size, named_tensors)

    def nparray_to_named_tensor(self, tensor_key, nparray):
        """
        Construct the NamedTensor Protobuf.

        Includes logic to create delta, compress tensors with the TensorCodec, etc.
        """
        # if we have an aggregated tensor, we can make a delta
        tensor_name, origin, round_number, report, tags = tensor_key
        if 'trained' in tags and self.delta_updates:
            # Should get the pretrained model to create the delta. If training
            # has happened,
            # Model should already be stored in the TensorDB
            model_nparray = self.tensor_db.get_tensor_from_cache(
                TensorKey(
                    tensor_name,
                    origin,
                    round_number,
                    report,
                    ('model',)
                )
            )

            # The original model will not be present for the optimizer on the
            # first round.
            if model_nparray is not None:
                delta_tensor_key, delta_nparray = self.tensor_codec.generate_delta(
                    tensor_key,
                    nparray,
                    model_nparray
                )
                delta_comp_tensor_key, delta_comp_nparray, metadata = self.tensor_codec.compress(
                    delta_tensor_key,
                    delta_nparray,
                    self.nn
                )

                named_tensor = utils.construct_named_tensor(
                    delta_comp_tensor_key,
                    delta_comp_nparray,
                    metadata,
                    lossless=False
                )
                return named_tensor

        # Assume every other tensor requires lossless compression
        compressed_tensor_key, compressed_nparray, metadata = self.tensor_codec.compress(
            tensor_key,
            nparray,
            require_lossless=True
        )
        named_tensor = utils.construct_named_tensor(
            compressed_tensor_key,
            compressed_nparray,
            metadata,
            lossless=True
        )

        return named_tensor

    def get_numpy_dict_for_tensorkeys(self, tensor_keys, **kwargs):
        """Get tensor dictionary for specified tensorkey set."""
        return {k.tensor_name: self.get_data_for_tensorkey(k, **kwargs) for k in tensor_keys}

    def get_data_for_tensorkey(self, tensor_key, **kwargs):
        """
        Resolve the tensor corresponding to the requested tensorkey.

        Args
        ----
        tensor_key:         Tensorkey that will be resolved locally or
                            remotely. May be the product of other tensors
        """
        # try to get from the store
        tensor_name, origin, round_number, report, tags = tensor_key

        nparray = None

        if 'model' in tags:
            # Pulling the model for the first time
            nparray = self.get_aggregated_tensor_from_aggregator(
                tensor_key,
                require_lossless=True
            )
        # @TODO: too much ad-hoc
        elif 'weak_learner' in tags:
            nparray = self.get_tensor_from_aggregator(
                tensor_key,
                require_lossless=True
            )
        elif 'adaboost_coeff' in tags:
            nparray = self.get_tensor_from_aggregator(
                tensor_key,
                require_lossless=True
            )

        if origin == self.collaborator_name:
            self.logger.debug(f'Attempting to retrieve tensor {tensor_key} from local store')
            nparray = self.tensor_db.get_tensor_from_cache(tensor_key)

            if nparray is None:
                if origin == self.collaborator_name:
                    self.logger.info(
                        f'Attempting to find locally stored {tensor_name} tensor from prior round...'
                    )
                    prior_round = round_number - 1
                    while prior_round >= 0:
                        nparray = self.tensor_db.get_tensor_from_cache(
                            TensorKey(tensor_name, origin, prior_round, report, tags))
                        if nparray is not None:
                            self.logger.debug(f'Found tensor {tensor_name} in local TensorDB '
                                              f'for round {prior_round}')
                            return nparray
                        prior_round -= 1
                    self.logger.info(
                        f'Cannot find any prior version of tensor {tensor_name} locally...'
                    )
                else:
                    self.logger.debug(f'Found tensor {tensor_key} in local TensorDB')
        else:
            # if None and origin is our client, request it from the client
            # self.logger.debug('Unable to get tensor from local store...'
            #                  'attempting to retrieve from client')
            # Determine whether there are additional compression related
            # dependencies.
            # Typically, dependencies are only relevant to model layers
            self.logger.debug('Retrieving tensor from client')
            tensor_dependencies = self.tensor_codec.find_dependencies(
                tensor_key, self.delta_updates
            )
            if len(tensor_dependencies) > 0:
                # Resolve dependencies
                # tensor_dependencies[0] corresponds to the prior version
                # of the model.
                # If it exists locally, should pull the remote delta because
                # this is the least costly path
                prior_model_layer = self.tensor_db.get_tensor_from_cache(
                    tensor_dependencies[0]
                )
                if prior_model_layer is not None:
                    uncompressed_delta = self.get_aggregated_tensor_from_aggregator(
                        tensor_dependencies[1]
                    )
                    new_model_tk, nparray = self.tensor_codec.apply_delta(
                        tensor_dependencies[1],
                        uncompressed_delta,
                        prior_model_layer,
                        creates_model=True,
                    )
                    self.tensor_db.cache_tensor({new_model_tk: nparray})
                else:
                    self.logger.info('Count not find previous model layer.'
                                     'Fetching latest layer from aggregator')
                    # The original model tensor should be fetched from client
                    nparray = self.get_aggregated_tensor_from_aggregator(
                        tensor_key,
                        require_lossless=True
                    )

        return nparray

    def get_tensor_from_aggregator(self, tensor_key,
                                   require_lossless=False):
        """
        Return the decompressed tensor associated with the requested tensor key.

        If the key requests a compressed tensor (in the tag), the tensor will
        be decompressed before returning
        If the key specifies an uncompressed tensor (or just omits a compressed
        tag), the decompression operation will be skipped

        Args
        ----
        tensor_key  :               The requested tensor
        require_lossless:   Should compression of the tensor be allowed
                                    in flight?
                                    For the initial model, it may affect
                                    convergence to apply lossy
                                    compression. And metrics shouldn't be
                                    compressed either

        Returns
        -------
        nparray     : The decompressed tensor associated with the requested
                      tensor key
        """
        tensor_name, origin, round_number, report, tags = tensor_key

        self.logger.debug(f'Requesting tensor {tensor_key}')
        tensor = self.client.get_tensor(
            self.collaborator_name, tensor_name, round_number, report, tags, require_lossless)

        # this translates to a numpy array and includes decompression, as
        # necessary
        nparray = self.named_tensor_to_nparray(tensor)

        # cache this tensor
        self.tensor_db.cache_tensor({tensor_key: nparray})

        return nparray

    def get_aggregated_tensor_from_aggregator(self, tensor_key,
                                              require_lossless=False):
        """
        Return the decompressed tensor associated with the requested tensor key.

        If the key requests a compressed tensor (in the tag), the tensor will
        be decompressed before returning
        If the key specifies an uncompressed tensor (or just omits a compressed
        tag), the decompression operation will be skipped

        Args
        ----
        tensor_key  :               The requested tensor
        require_lossless:   Should compression of the tensor be allowed
                                    in flight?
                                    For the initial model, it may affect
                                    convergence to apply lossy
                                    compression. And metrics shouldn't be
                                    compressed either

        Returns
        -------
        nparray     : The decompressed tensor associated with the requested
                      tensor key
        """
        tensor_name, origin, round_number, report, tags = tensor_key

        self.logger.debug(f'Requesting aggregated tensor {tensor_key}')
        tensor = self.client.get_aggregated_tensor(
            self.collaborator_name, tensor_name, round_number + 1, report, tags, require_lossless)

        # this translates to a numpy array and includes decompression, as
        # necessary
        nparray = self.named_tensor_to_nparray(tensor)

        # cache this tensor
        self.tensor_db.cache_tensor({tensor_key: nparray})

        return nparray

    def named_tensor_to_nparray(self, named_tensor):
        """Convert named tensor to a numpy array."""
        # do the stuff we do now for decompression and frombuffer and stuff
        # This should probably be moved back to protoutils
        raw_bytes = named_tensor.data_bytes
        metadata = [{'int_to_float': proto.int_to_float,
                     'int_list': proto.int_list,
                     'bool_list': proto.bool_list,
                     'model': proto.model,
                     } for proto in named_tensor.transformer_metadata]
        # The tensor has already been transfered to collaborator, so
        # the newly constructed tensor should have the collaborator origin
        tensor_key = TensorKey(
            named_tensor.name,
            self.collaborator_name,
            named_tensor.round_number,
            named_tensor.report,
            tuple(named_tensor.tags)
        )
        tensor_name, origin, round_number, report, tags = tensor_key
        if 'compressed' in tags:
            decompressed_tensor_key, decompressed_nparray = self.tensor_codec.decompress(
                tensor_key,
                data=raw_bytes,
                transformer_metadata=metadata,
                require_lossless=True
            )
        elif 'lossy_compressed' in tags:
            decompressed_tensor_key, decompressed_nparray = self.tensor_codec.decompress(
                tensor_key,
                data=raw_bytes,
                transformer_metadata=metadata
            )
        else:
            # There could be a case where the compression pipeline is bypassed
            # entirely
            self.logger.warning('Bypassing tensor codec...')
            decompressed_tensor_key = tensor_key
            decompressed_nparray = raw_bytes

        self.tensor_db.cache_tensor(
            {decompressed_tensor_key: decompressed_nparray}
        )

        return decompressed_nparray
