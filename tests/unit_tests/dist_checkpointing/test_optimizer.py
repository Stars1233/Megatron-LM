# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
from copy import deepcopy
from functools import partial
from time import sleep
from unittest import mock

import pytest
import torch
from torch.optim import Adam

from megatron.core import parallel_state
from megatron.core.dist_checkpointing import (
    ShardedTensor,
    load,
    load_plain_tensors,
    load_tensors_metadata,
    save,
)
from megatron.core.dist_checkpointing.dict_utils import diff, nested_values
from megatron.core.dist_checkpointing.optimizer import (
    get_param_id_to_sharded_param_map,
    optim_state_to_sharding_state,
)
from megatron.core.dist_checkpointing.serialization import get_default_save_sharded_strategy
from megatron.core.dist_checkpointing.strategies.fully_parallel import (
    FullyParallelSaveStrategyWrapper,
)
from megatron.core.dist_checkpointing.utils import extract_sharded_tensors
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_decoder_block_spec
from megatron.core.models.gpt.gpt_layer_specs import (
    get_gpt_layer_with_transformer_engine_spec as gpt_te_spec,
)
from megatron.core.models.gpt.gpt_model import GPTModel
from megatron.core.tensor_parallel import model_parallel_cuda_manual_seed
from megatron.core.transformer import MLATransformerConfig, TransformerConfig
from megatron.core.transformer.mlp import apply_swiglu_sharded_factory
from megatron.training.arguments import parse_args
from megatron.training.checkpointing import load_checkpoint, save_checkpoint
from tests.unit_tests.dist_checkpointing import (
    TempNamedDir,
    init_basic_mock_args,
    init_checkpointing_mock_args,
    initialize_gpt_model,
    setup_model_and_optimizer,
    setup_moe_model_and_optimizer,
)
from tests.unit_tests.test_utilities import Utils


class Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv = torch.nn.Conv1d(8, 16, 3)
        self.proj = torch.nn.Linear(8, 5)
        self.config = TransformerConfig(hidden_size=8, num_attention_heads=1, num_layers=1)

    def sharded_state_dict(self):
        sharded_state_dict = self.state_dict(keep_vars=True)
        # conv
        sharded_state_dict['conv.weight'] = ShardedTensor.from_rank_offsets(
            'conv.weight',
            sharded_state_dict['conv.weight'],
            (
                1,
                parallel_state.get_tensor_model_parallel_rank(),
                parallel_state.get_tensor_model_parallel_world_size(),
            ),
        )
        # bias is non-sharded
        sharded_state_dict['conv.bias'] = ShardedTensor.from_rank_offsets(
            'conv.bias', sharded_state_dict['conv.bias']
        )

        # proj
        sharded_state_dict['proj.weight'] = ShardedTensor.from_rank_offsets(
            'proj.weight', sharded_state_dict['proj.weight'], (0, Utils.rank, Utils.world_size)
        )
        sharded_state_dict['proj.bias'] = ShardedTensor.from_rank_offsets(
            'proj.bias', sharded_state_dict['proj.bias'], (0, Utils.rank, Utils.world_size)
        )
        return sharded_state_dict


class SwigluFactoryModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.linear = torch.nn.Linear(
            5, 64 // parallel_state.get_tensor_model_parallel_world_size(), bias=False
        )
        self.config = TransformerConfig(hidden_size=8, num_attention_heads=1, num_layers=1)

    def sharded_state_dict(self):
        sharded_state_dict = self.state_dict(keep_vars=True)
        sharded_state_dict['linear.weight'] = ShardedTensor.from_rank_offsets(
            'linear.weight',
            sharded_state_dict['linear.weight'],
            (
                (
                    0,
                    parallel_state.get_tensor_model_parallel_rank(),
                    parallel_state.get_tensor_model_parallel_world_size(),
                )
            ),
            replica_id=(
                (
                    parallel_state.get_pipeline_model_parallel_rank(),
                    0,
                    parallel_state.get_data_parallel_rank(with_context_parallel=True),
                )
            ),
        )
        sharded_state_dict['linear.weight'] = apply_swiglu_sharded_factory(
            sharded_state_dict['linear.weight'], ()
        )
        return sharded_state_dict


class Model1dFlattenTensor(torch.nn.Module):
    """This model is used to test whether a 1d flatten tensor can be correctly
    transformed into torch dist-ckpt form
    """

    def __init__(self):
        super().__init__()
        self.config = TransformerConfig(hidden_size=128, num_attention_heads=1, num_layers=1)
        self.weight_1d = torch.nn.Parameter(torch.randn(self.config.hidden_size))

    def sharded_state_dict(self):
        sharded_state_dict = self.state_dict(keep_vars=True)
        sharded_state_dict['weight_1d'] = ShardedTensor.from_rank_offsets(
            'weight_1d',
            sharded_state_dict['weight_1d'],
            (
                (
                    0,
                    parallel_state.get_tensor_model_parallel_rank(),
                    parallel_state.get_tensor_model_parallel_world_size(),
                )
            ),
            replica_id=(
                (
                    parallel_state.get_pipeline_model_parallel_rank(),
                    0,
                    parallel_state.get_data_parallel_rank(with_context_parallel=True),
                )
            ),
        )
        return sharded_state_dict


class TestOptimizer:
    def setup_method(self, method):
        pass

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    def test_optimizer_params(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(1, 1)
        model = Model()
        # Force optimizer state initialization
        for p in model.parameters():
            p.grad = torch.ones_like(p.data)
        optim = Adam(model.parameters())
        optim.step()

        model_state_dict = model.sharded_state_dict()
        param_map = get_param_id_to_sharded_param_map(
            model_state_dict, optim.param_groups[0]['params']
        )
        optim_state_dict = optim.state_dict()
        optim_state_to_sharding_state(optim_state_dict, param_map, exclude_keys=('step',))

        optim_sharded_tensors = nested_values(extract_sharded_tensors(optim_state_dict)[0])
        optim_sharded_keys = {sh_ten.key for sh_ten in optim_sharded_tensors}
        assert len(optim_sharded_keys) == 2 * len(model_state_dict)
        assert optim_sharded_keys == set(
            [
                f'optimizer.state.{state_key}.{layer_name}'
                for state_key in ['exp_avg', 'exp_avg_sq']
                for layer_name in model_state_dict
            ]
        )


def initialize_small_model(pre_process=True, post_process=True, seed=0, **config_kwargs):
    torch.manual_seed(seed)
    model_parallel_cuda_manual_seed(seed)

    return SwigluFactoryModel()


def initialize_1d_flatten_tensor_model(
    pre_process=True, post_process=True, seed=0, **config_kwargs
):
    # This model is used to test whether a 1d flatten tensor can be correctly
    # transformed into torch dist-ckpt form
    torch.manual_seed(seed)
    model_parallel_cuda_manual_seed(seed)

    return Model1dFlattenTensor()


def initialize_real_model(
    seed,
    pre_process,
    post_process,
    vp_stage=None,
    is_moe=False,
    is_mla=False,
    virtual_pipeline_model_parallel_size=None,
    **config_kwargs,
):
    torch.manual_seed(seed)
    model_parallel_cuda_manual_seed(seed)

    default_config_kwargs = dict(
        num_layers=6,
        hidden_size=16,
        num_attention_heads=8,
        use_cpu_initialization=True,
        pipeline_dtype=torch.bfloat16,
        bf16=True,
        virtual_pipeline_model_parallel_size=virtual_pipeline_model_parallel_size,
    )
    if is_moe:
        default_config_kwargs["moe_ffn_hidden_size"] = 128
        default_config_kwargs["num_moe_experts"] = 4
        default_config_kwargs["add_bias_linear"] = False
        # Pop unused fields
        config_kwargs.pop("use_sp")
        config_kwargs.pop("use_te")
        config_kwargs.pop("use_grouped_mlp")
        config_kwargs.pop("use_glu")
    if is_mla:
        default_config_kwargs["multi_latent_attention"] = True
        default_config_kwargs["q_lora_rank"] = 96
        default_config_kwargs["kv_lora_rank"] = 512
        default_config_kwargs["qk_head_dim"] = 64
        default_config_kwargs["qk_pos_emb_head_dim"] = 32
        default_config_kwargs["v_head_dim"] = 64
    default_config_kwargs.update(**config_kwargs)
    config_cls = MLATransformerConfig if is_mla else TransformerConfig
    transformer_config = config_cls(**default_config_kwargs)

    if is_moe:
        layer_spec = get_gpt_decoder_block_spec(
            transformer_config, use_transformer_engine=True, vp_stage=vp_stage
        )
    else:
        layer_spec = gpt_te_spec(multi_latent_attention=is_mla)
    this_model = GPTModel(
        config=transformer_config,
        transformer_layer_spec=layer_spec,
        vocab_size=128,
        max_sequence_length=4,
        pre_process=pre_process,
        post_process=post_process,
        vp_stage=vp_stage,
    )

    return this_model


def load_checkpoint_no_arg_checks(*args, **kwargs):
    with mock.patch('megatron.training.checkpointing.check_checkpoint_args'):
        with mock.patch('megatron.training.checkpointing.update_num_microbatches'):
            return load_checkpoint(*args, **kwargs)


class TestDistributedOptimizer:
    def setup_method(self, method):
        pass

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    @pytest.mark.parametrize("fully_parallel", [False, True])
    @pytest.mark.parametrize(
        ("tp_pp_ep", "is_moe", "is_mla", "test_step", "kwargs"),
        [
            ((2, 2, 1), False, False, False, {}),  # check TP
            ((1, 2, 1), False, False, True, {}),  # check "step" is synced
            ((1, 2, 1), False, True, False, {}),  # check param group order is right
            (
                (1, 8, 1),
                False,
                False,
                False,
                {
                    "account_for_embedding_in_pipeline_split": True,
                    "account_for_loss_in_pipeline_split": True,
                },
            ),  # check embedding standalone
            (
                (1, 2, 2),
                True,
                False,
                True,
                {"moe_layer_freq": [0, 0, 0, 1, 1, 1]},
            ),  # check moe not on all ranks (case 1)
            (
                (1, 2, 2),
                True,
                False,
                True,
                {"moe_layer_freq": [1, 1, 1, 0, 0, 0]},
            ),  # check moe not on all ranks (case 2)
        ],
    )
    def test_optimizer_common_state_dict(
        self, tmp_path_dist_ckpt, fully_parallel, tp_pp_ep, is_moe, is_mla, test_step, kwargs
    ):
        initialize_fn = partial(initialize_real_model, is_moe=is_moe, is_mla=is_mla, **kwargs)

        # Initialize parallel
        tp, pp, ep = tp_pp_ep
        Utils.initialize_model_parallel(
            tensor_model_parallel_size=tp,
            pipeline_model_parallel_size=pp,
            expert_model_parallel_size=ep,
        )
        rank = torch.distributed.get_rank()

        with TempNamedDir(tmp_path_dist_ckpt / 'test_dp_sharding', sync=True) as ckpt_dir:
            mock_args = parse_args(ignore_unknown_args=True)
            mock_args.use_distributed_optimizer = True
            with mock.patch('megatron.training.checkpointing.get_args', new=lambda: mock_args):
                # Initialize model and optimizer A
                if is_moe:
                    model, optimizer_A = setup_moe_model_and_optimizer(
                        seed=2, tp=tp, pp=pp, ep=ep, initialize_fn=initialize_fn
                    )
                else:
                    model, optimizer_A = setup_model_and_optimizer(
                        seed=2, tp=tp, pp=pp, initialize_fn=initialize_fn
                    )
                if test_step:
                    # Simulate "step" not set in some of the param groups on rank 0.
                    # TE FusedAdam may have "step" not set in some of the param groups on some ranks.
                    for i, param_group in enumerate(
                        optimizer_A.chained_optimizers[0].optimizer.param_groups
                    ):
                        if rank > 0 or i == 0:
                            param_group['step'] = 1234

                # Save checkpoint
                init_checkpointing_mock_args(mock_args, ckpt_dir, fully_parallel=fully_parallel)
                from megatron.training.training import preprocess_common_state_dict

                save_checkpoint(
                    10,
                    model,
                    optimizer_A,
                    None,
                    0,
                    preprocess_common_state_dict_fn=preprocess_common_state_dict,
                )

                # Get optimizer A param state
                optim_param_state_A = optimizer_A.state_dict()

                # Initialize model and optimizer B
                if is_moe:
                    model, optimizer_B = setup_moe_model_and_optimizer(
                        seed=3, tp=tp, pp=pp, ep=ep, initialize_fn=initialize_fn
                    )
                else:
                    model, optimizer_B = setup_model_and_optimizer(
                        seed=3, tp=tp, pp=pp, initialize_fn=initialize_fn
                    )
                # Load optimizer B from checkpoint
                load_checkpoint_no_arg_checks(model, optimizer_B, None)
                if test_step:
                    # Complete "step" for comparison
                    for i, param_group in enumerate(
                        optimizer_A.chained_optimizers[0].optimizer.param_groups
                    ):
                        if rank == 0 and i > 0:
                            param_group['step'] = 1234
                # Get optimizer B param state
                optim_param_state_B = optimizer_B.state_dict()

                # Test both param state dicts are equal
                diffs = diff(optim_param_state_A, optim_param_state_B)
                assert not any(map(bool, diffs)), (rank, diffs)

        Utils.destroy_model_parallel()

    @pytest.mark.parametrize(
        "initialize_fn",
        [initialize_small_model, initialize_gpt_model, initialize_1d_flatten_tensor_model],
    )
    @pytest.mark.parametrize("use_fpsl", [False, True])
    # TODO: changing DP doesn't work in unit tests because of NCCL crashes
    @pytest.mark.parametrize(
        "tp_pp,src_dp,dest_dp",
        [
            ((4, 1), 2, 2),
            # ((1, 1), 8, 1),
            # ((1, 1), 1, 8),
            # ((2, 1), 2, 1),
            # ((2, 1), 2, 2),
        ],
    )
    @pytest.mark.flaky
    @pytest.mark.flaky_in_dev
    def test_dp_sharding(self, tmp_path_dist_ckpt, tp_pp, src_dp, dest_dp, use_fpsl, initialize_fn):
        src_world_size = tp_pp[0] * tp_pp[1] * src_dp
        dest_world_size = tp_pp[0] * tp_pp[1] * dest_dp
        assert src_world_size <= Utils.world_size, (tp_pp, src_dp)
        assert dest_world_size <= Utils.world_size, (tp_pp, dest_dp)

        sharding_type = 'fully_sharded_model_space' if use_fpsl else 'dp_zero_gather_scatter'

        Utils.initialize_model_parallel(*tp_pp)

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / 'test_dp_sharding', sync=True) as ckpt_dir:
            try:
                Utils.set_world_size(src_world_size)
                if Utils.rank >= 0:
                    # Save checkpoint A
                    model, optimizer_A = setup_model_and_optimizer(
                        seed=2, tp=tp_pp[0], pp=tp_pp[1], initialize_fn=initialize_fn
                    )

                    save_strategy = get_default_save_sharded_strategy()
                    if use_fpsl:
                        save_strategy = FullyParallelSaveStrategyWrapper(
                            save_strategy,
                            parallel_state.get_data_parallel_group(with_context_parallel=True),
                            True,
                        )
                    optim_state_dict = optimizer_A.sharded_state_dict(
                        model[0].sharded_state_dict(), sharding_type=sharding_type
                    )
                    save(optim_state_dict, ckpt_dir, save_strategy)
                    optim_param_state_A = optimizer_A.get_parameter_state_dp_zero()
                    Utils.destroy_model_parallel()
                else:
                    # this prevents NCCL errors when changing DP. TODO: fix it properly
                    sleep(20)

                # Load checkpoint A with different TP/PP and save as checkpoint B
                Utils.set_world_size(dest_world_size)
                if Utils.rank == 0:
                    print('_____________________')
                if Utils.rank >= 0:
                    Utils.initialize_model_parallel(*tp_pp)

                    model, optimizer_B = setup_model_and_optimizer(
                        seed=3, tp=tp_pp[0], pp=tp_pp[1], initialize_fn=initialize_fn
                    )
                    optim_param_state_B = optimizer_B.get_parameter_state_dp_zero()
                    diffs = diff(optim_param_state_A, optim_param_state_B)
                    # Expect a mismatch in values - diffs[2] nonempty
                    if parallel_state.get_data_parallel_rank(with_context_parallel=True) == 0:
                        assert not diffs[0] and not diffs[1] and diffs[2], diffs

                    sharded_state_dict = optimizer_B.sharded_state_dict(
                        model[0].sharded_state_dict(), is_loading=True, sharding_type=sharding_type
                    )
                    optim_state_dict = load(sharded_state_dict, ckpt_dir)
                    optimizer_B.load_state_dict(optim_state_dict)
                    optim_param_state_B = optimizer_B.get_parameter_state_dp_zero()

                    # Test both param state dicts are equal
                    diffs = diff(optim_param_state_A, optim_param_state_B)
                    assert not any(map(bool, diffs)), diffs
                else:
                    # this prevents NCCL errors when changing DP. TODO: fix it properly
                    sleep(20)
            finally:
                Utils.set_world_size()

    @pytest.mark.parametrize(
        ('src_tp_pp', 'dest_tp_pp', 'use_glu'),
        [((2, 2), (2, 4), False), ((1, 8), (4, 1), True), ((2, 4), (4, 2), False)],
    )
    def test_finetune_doesnt_load_optimizer(
        self, tmp_path_dist_ckpt, src_tp_pp, dest_tp_pp, use_glu
    ):
        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        Utils.initialize_model_parallel(*src_tp_pp)
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_finetune_doesnt_load_optimizer', sync=True
        ) as ckpt_dir:
            mock_args = parse_args(ignore_unknown_args=True)
            with mock.patch('megatron.training.checkpointing.get_args', new=lambda: mock_args):
                init_basic_mock_args(mock_args, tp=src_tp_pp[0], pp=src_tp_pp[1])
                init_checkpointing_mock_args(mock_args, ckpt_dir, False)

                model, optimizer = setup_model_and_optimizer(
                    seed=2,
                    tp=src_tp_pp[0],
                    pp=src_tp_pp[1],
                    initialize_fn=partial(initialize_gpt_model, use_glu=use_glu),
                )

                save_checkpoint(10, model, optimizer, None, 0)
                Utils.destroy_model_parallel()

                Utils.initialize_model_parallel(*dest_tp_pp)
                mock_args.tensor_model_parallel_size = dest_tp_pp[0]
                mock_args.pipeline_model_parallel_size = dest_tp_pp[1]
                model, optimizer = setup_model_and_optimizer(
                    seed=3,
                    tp=dest_tp_pp[0],
                    pp=dest_tp_pp[1],
                    initialize_fn=partial(initialize_gpt_model, use_glu=use_glu),
                )
                model_unloaded_state_dict = deepcopy(model[0].state_dict())
                optim_unloaded_state_dict = deepcopy(optimizer.state_dict())

                # Load with different TPxPP should raise DistributeOptimizer error
                with pytest.raises(RuntimeError) as exc_info:
                    load_checkpoint_no_arg_checks(model, optimizer, None)
                # "(TP, PP) mismatch" check is for backwards compatibility tests
                assert "(TP, PP) mismatch" in str(
                    exc_info.value
                ) or "(TP, PP, encoder TP, encoder PP) mismatch" in str(exc_info.value)

                # Check that the state didn't change
                assert not any(diff(model[0].state_dict(), model_unloaded_state_dict))
                assert not any(diff(optimizer.state_dict(), optim_unloaded_state_dict))

                # Now test the same with a `finetune` flag
                mock_args.finetune = True
                load_checkpoint_no_arg_checks(model, optimizer, None)

                # Model weights should be different, but optimizer state is unchanged
                diffs = diff(model[0].state_dict(), model_unloaded_state_dict)
                # diffs[0] and diffs[1] is structural diff, diffs[2] is values diff -
                # we expect only values diff
                assert not diffs[0] and not diffs[1] and diffs[2]
                assert not any(diff(optimizer.state_dict(), optim_unloaded_state_dict))

                # ... or `no_load_optim` flag
                model, optimizer = setup_model_and_optimizer(
                    seed=3,
                    tp=dest_tp_pp[0],
                    pp=dest_tp_pp[1],
                    initialize_fn=partial(initialize_gpt_model, use_glu=use_glu),
                )
                mock_args.finetune = False
                mock_args.no_load_optim = True
                mock_args.no_load_rng = True
                load_checkpoint_no_arg_checks(model, optimizer, None)

                # Model weights should be different, but optimizer state is unchanged
                diffs = diff(model[0].state_dict(), model_unloaded_state_dict)
                # diffs[0] and diffs[1] is structural diff, diffs[2] is values diff -
                # we expect only values diff
                assert not diffs[0] and not diffs[1] and diffs[2]
                assert not any(diff(optimizer.state_dict(), optim_unloaded_state_dict))


class TestFP32Optimizer:
    def setup_method(self, method):
        pass

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    @pytest.mark.parametrize(
        ('src_tp_pp', 'dest_tp_pp'), [((2, 4), (2, 4)), ((2, 4), (4, 2)), ((8, 1), (1, 2))]
    )
    def test_fp32_optimizer_resharding(self, tmp_path_dist_ckpt, src_tp_pp, dest_tp_pp):
        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.

        def preprocess_fn(optim_common_dict):
            import copy

            preprocessed_optimzier_common_dict = copy.deepcopy(optim_common_dict)
            list = preprocessed_optimzier_common_dict['optimizer']['param_groups']
            for dict_item in list:
                del dict_item['wd_mult']
            return preprocessed_optimzier_common_dict

        Utils.initialize_model_parallel(*src_tp_pp)
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_A', sync=True
        ) as ckpt_dir_A:
            with TempNamedDir(
                tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_B', sync=True
            ) as ckpt_dir_B:

                model_A, optimizer_A = setup_model_and_optimizer(
                    seed=2,
                    tp=src_tp_pp[0],
                    pp=src_tp_pp[1],
                    initialize_fn=initialize_small_model,
                    bf16=False,
                )

                save(
                    optimizer_A.sharded_state_dict(model_A[0].sharded_state_dict()),
                    ckpt_dir_A,
                    preprocess_common_before_consistancy_check=preprocess_fn,
                )
                Utils.destroy_model_parallel()

                # Load checkpoint A with different TP/PP and save as checkpoint B
                Utils.initialize_model_parallel(*dest_tp_pp)
                model_B, optimizer_B = setup_model_and_optimizer(
                    seed=3,
                    tp=dest_tp_pp[0],
                    pp=dest_tp_pp[1],
                    initialize_fn=initialize_small_model,
                    bf16=False,
                )
                load_sharded_state_dict = optimizer_B.sharded_state_dict(
                    model_B[0].sharded_state_dict()
                )
                state_dict = load(load_sharded_state_dict, ckpt_dir_A)

                optimizer_B.load_state_dict(state_dict)
                save(optimizer_B.sharded_state_dict(model_B[0].sharded_state_dict()), ckpt_dir_B)
                Utils.destroy_model_parallel()

                # Test both checkpoints are equal
                Utils.initialize_model_parallel(1, 1)
                plain_state_dict_A = load_plain_tensors(ckpt_dir_A)
                plain_state_dict_B = load_plain_tensors(ckpt_dir_B)
                diffs = diff(plain_state_dict_A, plain_state_dict_B)
                assert not any(map(bool, diffs)), diffs


class TestOptimizerResharding:
    def setup_method(self, method):
        pass

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    @pytest.mark.parametrize(
        ('use_dist_opt', 'bf16', 'use_custom_fsdp'),
        (
            (False, True, False),  # regular BF16
            (True, True, False),  # DistOpt BF16
            (True, True, True),  # DistOpt + custom FSDP BF16
            # (False, False), # FP32
        ),
    )
    @pytest.mark.parametrize(
        ('src_tp_pp', 'dest_tp_pp'),
        [((2, 4), (2, 4)), ((2, 4), (2, 2)), ((2, 4), (4, 2)), ((8, 1), (1, 2))],
    )
    def test_optimizer_resharding(
        self, tmp_path_dist_ckpt, src_tp_pp, dest_tp_pp, use_dist_opt, bf16, use_custom_fsdp
    ):
        Utils.initialize_model_parallel(*src_tp_pp)
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_A', sync=False
        ) as ckpt_dir_A:
            with TempNamedDir(
                tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_B', sync=False
            ) as ckpt_dir_B:
                extra_kwargs = {}
                if use_custom_fsdp:
                    extra_kwargs['use_custom_fsdp'] = True

                model_A, optimizer_A = setup_model_and_optimizer(
                    seed=2, tp=src_tp_pp[0], pp=src_tp_pp[1], bf16=bf16, dist_opt=use_dist_opt
                )

                save(optimizer_A.sharded_state_dict(model_A[0].sharded_state_dict()), ckpt_dir_A)
                Utils.destroy_model_parallel()

                # Load checkpoint A with different TP/PP and save as checkpoint B
                Utils.initialize_model_parallel(*dest_tp_pp)
                model_B, optimizer_B = setup_model_and_optimizer(
                    seed=3, tp=dest_tp_pp[0], pp=dest_tp_pp[1], bf16=bf16, dist_opt=use_dist_opt
                )
                load_sharded_state_dict = optimizer_B.sharded_state_dict(
                    model_B[0].sharded_state_dict()
                )
                state_dict = load(load_sharded_state_dict, ckpt_dir_A)

                optimizer_B.load_state_dict(state_dict)
                save(optimizer_B.sharded_state_dict(model_B[0].sharded_state_dict()), ckpt_dir_B)
                Utils.destroy_model_parallel()

                # Test both checkpoints are equal
                Utils.initialize_model_parallel(1, 1)
                plain_state_dict_A = load_plain_tensors(ckpt_dir_A)
                plain_state_dict_B = load_plain_tensors(ckpt_dir_B)
                diffs = diff(plain_state_dict_A, plain_state_dict_B)
                assert not any(map(bool, diffs)), diffs

                if use_custom_fsdp and hasattr(torch.nn.parameter.Parameter, "main_grad"):
                    # Custom fsdp adds the `main_grad` attribute function to the
                    # torch Parameter, remove this attribute function so that
                    # it doesn't conflict with the code in the non-custom fsdp
                    # test branch.
                    delattr(torch.nn.parameter.Parameter, "main_grad")

    @pytest.mark.parametrize(('use_dist_opt', 'bf16'), ((True, True),))  # DistOpt BF16
    @pytest.mark.parametrize(('use_te', 'use_grouped_mlp'), ((False, False), (False, True)))
    @pytest.mark.parametrize('use_glu', [False, True])
    @pytest.mark.parametrize(
        ('src_tp_pp_exp', 'dest_tp_pp_exp'),
        [
            ((2, 2, 2), (2, 2, 2)),
            ((4, 1, 2), (1, 2, 2)),
            ((1, 1, 2), (1, 1, 4)),
            ((2, 1, 2), (1, 1, 8)),
        ],
    )
    def test_chained_optimizer_resharding(
        self,
        tmp_path_dist_ckpt,
        src_tp_pp_exp,
        dest_tp_pp_exp,
        use_dist_opt,
        bf16,
        use_te,
        use_grouped_mlp,
        use_glu,
    ):
        src_tp, src_pp, src_exp = src_tp_pp_exp
        dest_tp, dest_pp, dest_exp = dest_tp_pp_exp
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_A', sync=False
        ) as ckpt_dir_A:
            with TempNamedDir(
                tmp_path_dist_ckpt / 'test_fp32_optimizer_state_dict_B', sync=False
            ) as ckpt_dir_B:
                Utils.initialize_model_parallel(src_tp, src_pp, expert_model_parallel_size=src_exp)
                model_A, optimizer_A = setup_moe_model_and_optimizer(
                    seed=2,
                    tp=src_tp,
                    pp=src_pp,
                    ep=src_exp,
                    bf16=bf16,
                    dist_opt=use_dist_opt,
                    use_te=use_te,
                    use_grouped_mlp=use_grouped_mlp,
                    use_glu=use_glu,
                )

                save(optimizer_A.sharded_state_dict(model_A[0].sharded_state_dict()), ckpt_dir_A)
                Utils.destroy_model_parallel()

                # Load checkpoint A with different TP/PP and save as checkpoint B
                Utils.initialize_model_parallel(
                    dest_tp, dest_pp, expert_model_parallel_size=dest_exp
                )
                model_B, optimizer_B = setup_moe_model_and_optimizer(
                    seed=3,
                    tp=dest_tp,
                    pp=dest_pp,
                    ep=dest_exp,
                    bf16=bf16,
                    dist_opt=use_dist_opt,
                    use_te=use_te,
                    use_grouped_mlp=use_grouped_mlp,
                    use_glu=use_glu,
                )
                load_sharded_state_dict = optimizer_B.sharded_state_dict(
                    model_B[0].sharded_state_dict()
                )
                state_dict = load(load_sharded_state_dict, ckpt_dir_A)

                optimizer_B.load_state_dict(state_dict)
                save(optimizer_B.sharded_state_dict(model_B[0].sharded_state_dict()), ckpt_dir_B)
                Utils.destroy_model_parallel()

                # Test both checkpoints are equal
                Utils.initialize_model_parallel(1, 1)
                plain_state_dict_A = load_plain_tensors(ckpt_dir_A)
                plain_state_dict_B = load_plain_tensors(ckpt_dir_B)
                diffs = diff(plain_state_dict_A, plain_state_dict_B)
                assert not any(map(bool, diffs)), diffs
                Utils.destroy_model_parallel()
