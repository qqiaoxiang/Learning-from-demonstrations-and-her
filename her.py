import os
import click
import numpy as np
import json
from mpi4py import MPI
from baselines import logger
from baselines.common import set_global_seeds, tf_util
from baselines.common.mpi_moments import mpi_moments
import baselines.her.experiment.config as config
from baselines.her.rollout import RolloutWorker


# Calculates the average of the given 'value' 
# and communicates and synchronizes it between multiple MPI processes.
def mpi_average(value):
    if not isinstance(value, list):
        value = [value]
    if not any(value):
        value = [0.]
    return mpi_moments(np.array(value))[0]

# Train the policy
def train(*, policy, rollout_worker, evaluator,
          n_epochs, n_test_rollouts, n_cycles, n_batches, policy_save_interval,
          save_path, demo_file, **kwargs):
    # Get the current process rank from the MPI's process No.
    rank = MPI.COMM_WORLD.Get_rank()

    # Define the path of latest policy, best policy, periodic policy
    if save_path:
        latest_policy_path = os.path.join(save_path, 'policy_latest.pkl')
        best_policy_path = os.path.join(save_path, 'policy_best.pkl')
        periodic_policy_path = os.path.join(save_path, 'policy_{}.pkl')

    logger.info("Training...")
    # Initial variables
    # Setting the initial value to '-1' is to ensure the first "success rate" during the training will be cosidered as the "best success rate",
    # since any "success rate" higher than '-1' will considered better.
    best_success_rate = -1

    # Initial demo buffer if the policy requires the use of behavior clone loss --> 'policy.bc_loss ==1'
    if policy.bc_loss == 1: policy.init_demo_buffer(demo_file)


    # Start training
    # num_timesteps = n_epochs * n_cycles * rollout_length * number of rollout workers
    for epoch in range(n_epochs):
    
        # clear history rollout(trajectory) record
        rollout_worker.clear_history()
        
        for _ in range(n_cycles):
            # generate rollouts and store them in the experience replay buffer
            episode = rollout_worker.generate_rollouts()
            policy.store_episode(episode)
            
            # training multiple batches(n_batches) and update the target network
            for _ in range(n_batches):
                policy.train()
            policy.update_target_net()

        # test
        logger.info("Testing")
        # clear history rollouts record and generate new rollouts
        evaluator.clear_history()
        for _ in range(n_test_rollouts):
            evaluator.generate_rollouts()

        # record logs
        logger.record_tabular('epoch', epoch)
        
        # record log of test phase
        for key, val in evaluator.logs('test'):
            logger.record_tabular(key, mpi_average(val))
            
        # record log of train phase
        for key, val in rollout_worker.logs('train'):
            logger.record_tabular(key, mpi_average(val))
            
        # record log of policy 
        for key, val in policy.logs():
            logger.record_tabular(key, mpi_average(val))

        # print logs out if the ranking of current process is 0.
        if rank == 0:
            logger.dump_tabular()

        # save the policy if it's better than the previous ones
        success_rate = mpi_average(evaluator.current_success_rate())
        
        if rank == 0 and success_rate >= best_success_rate and save_path:
            best_success_rate = success_rate
            logger.info('New best success rate: {}. Saving policy to {} ...'.format(best_success_rate, best_policy_path))
            evaluator.save_policy(best_policy_path)
            evaluator.save_policy(latest_policy_path)
        if rank == 0 and policy_save_interval > 0 and epoch % policy_save_interval == 0 and save_path:
            policy_path = periodic_policy_path.format(epoch)
            logger.info('Saving periodic policy to {} ...'.format(policy_path))
            evaluator.save_policy(policy_path)

        # make sure that different threads have different seeds
        logger.info("The best success rate so far ", best_success_rate)
        local_uniform = np.random.uniform(size=(1,))
        root_uniform = local_uniform.copy()
        MPI.COMM_WORLD.Bcast(root_uniform, root=0)
        
        if rank != 0:
            assert local_uniform[0] != root_uniform[0]

    return policy



def learn(*, network, env, total_timesteps,
    seed=None,
    eval_env=None,
    replay_strategy='future',
    policy_save_interval=5,
    clip_return=True,
    demo_file=None,
    override_params=None,
    load_path=None,
    save_path=None,
    **kwargs
):
    # network: specify the structure of the policy network and the value function network
    # env: OpenAI Gym, the training enviroment
    
    override_params = override_params or {}
    if MPI is not None:
        rank = MPI.COMM_WORLD.Get_rank()
        num_cpu = MPI.COMM_WORLD.Get_size()

    # Seed everything.
    rank_seed = seed + 1000000 * rank if seed is not None else None
    set_global_seeds(rank_seed)

    # Prepare params.
    params = config.DEFAULT_PARAMS
    env_name = env.spec.id
    params['env_name'] = env_name
    params['replay_strategy'] = replay_strategy
    
    if env_name in config.DEFAULT_ENV_PARAMS:
        # merge env-specific parameters in
        params.update(config.DEFAULT_ENV_PARAMS[env_name]) 
        
    # makes it possible to override any parameter   
    params.update(**override_params)  
    
    # Save the params
    with open(os.path.join(logger.get_dir(), 'params.json'), 'w') as f:
         json.dump(params, f)
    params = config.prepare_params(params)
    params['rollout_batch_size'] = env.num_envs

    if demo_file is not None:
        params['bc_loss'] = 1
    params.update(kwargs)

    config.log_params(params, logger=logger)

    if num_cpu == 1:
        logger.warn()
        logger.warn('*** Warning ***')
        logger.warn(
            'You are running HER with just a single MPI worker. This will work, but the ' +
            'experiments that we report in Plappert et al. (2018, https://arxiv.org/abs/1802.09464) ' +
            'were obtained with --num_cpu 19. This makes a significant difference and if you ' +
            'are looking to reproduce those results, be aware of this. Please also refer to ' +
            'https://github.com/openai/baselines/issues/314 for further details.')
        logger.warn('****************')
        logger.warn()

    # Configure dims & policy using the DDPG 
    dims = config.configure_dims(params)
    policy = config.configure_ddpg(dims=dims, params=params, clip_return=clip_return)
    if load_path is not None:
        tf_util.load_variables(load_path)

    rollout_params = {
        'exploit': False,
        'use_target_net': False,
        'use_demo_states': True,
        'compute_Q': False,
        'T': params['T'],
    }

    eval_params = {
        'exploit': True,
        'use_target_net': params['test_with_polyak'],
        'use_demo_states': False,
        'compute_Q': True,
        'T': params['T'],
    }

    for name in ['T', 'rollout_batch_size', 'gamma', 'noise_eps', 'random_eps']:
        rollout_params[name] = params[name]
        eval_params[name] = params[name]

    eval_env = eval_env or env

    # Interacting with the environment and evaluating policy performance
    # RolloutWorker: Sample the data and store it in the experience replay buffer of the policy. 
    # Then, train the policy in multiple batches in each cycle and update the target network.
    rollout_worker = RolloutWorker(env, policy, dims, logger, monitor=True, **rollout_params)
    # After each cycle, evaluate the policy, recorded in the log and output.
    evaluator = RolloutWorker(eval_env, policy, dims, logger, **eval_params)

    n_cycles = params['n_cycles']
    n_epochs = total_timesteps // n_cycles // rollout_worker.T // rollout_worker.rollout_batch_size

    return train(
        save_path=save_path, policy=policy, rollout_worker=rollout_worker,
        evaluator=evaluator, n_epochs=n_epochs, n_test_rollouts=params['n_test_rollouts'],
        n_cycles=params['n_cycles'], n_batches=params['n_batches'],
        policy_save_interval=policy_save_interval, demo_file=demo_file)


@click.command()
@click.option('--env', type=str, default='FetchPickAndPlace-v1', help='the name of the OpenAI Gym environment that you want to train on')
@click.option('--total_timesteps', type=int, default=int(5e5), help='the number of timesteps to run')
@click.option('--seed', type=int, default=0, help='the random seed used to seed both the environment and the training code')
@click.option('--policy_save_interval', type=int, default=5, help='the interval with which policy pickles are saved. If set to 0, only the best and latest policy will be pickled.')
@click.option('--replay_strategy', type=click.Choice(['future', 'none']), default='future', help='the HER replay strategy to be used. "future" uses HER, "none" disables HER.')
@click.option('--clip_return', type=int, default=1, help='whether or not returns should be clipped')
@click.option('--demo_file', type=str, default = '/home/safiyya/桌面/baselines-master/baselines/her/data/data_fetch_random_100.npz', help='demo data file path')

def main(**kwargs):
    learn(**kwargs)


if __name__ == '__main__':
    main()
