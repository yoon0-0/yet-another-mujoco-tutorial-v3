import os,cv2
import numpy as np
import mujoco
import mujoco_viewer
from util import pr2t,r2w,rpy2r,trim_scale,meters2xyz,compute_view_params
import ray
import time
from mujoco_parser import MuJoCoParserClass
import torch
from util import *
from pid import PID_ControllerClass
import sys
import gc
@ray.remote
class MuJoCoParserClassRay(MuJoCoParserClass):
    """
        MuJoCo Parser class
    """
    def __init__(self,name='Robot',rel_xml_path=None,USE_MUJOCO_VIEWER=False,VERBOSE=True,device='cpu',env_id=0,horizon_length=64,contact_body_ids=np.array([]),key_body_ids=np.array([]),motion_lib=None,max_episode_length=300):
        """
            Initialize MuJoCo parser with ray
        """
        super().__init__(name=name,rel_xml_path=rel_xml_path,USE_MUJOCO_VIEWER=USE_MUJOCO_VIEWER,VERBOSE=VERBOSE)

        self.PID = PID_ControllerClass(
                name = 'PID',dim = self.n_ctrl,
                k_p = 0.4, k_i = 0.01, k_d = 0.001,
                out_min = self.ctrl_ranges[:,0],
                out_max = self.ctrl_ranges[:,1],
                dt = 0.02,
                ANTIWU  = True)
        self.device = 'cpu'#device
        self.horizon_length = horizon_length
        self._contact_forces = torch.zeros((self.n_body, 3), device=self.device)
        self._contact_body_ids = contact_body_ids
        self._key_body_ids = key_body_ids
        self.reset_flag = torch.ones((1,))
        assert motion_lib is not None
        self._motion_lib = motion_lib
        self.max_episode_length = max_episode_length
        # Change floor friction
        self.model.geom('floor').friction = np.array([1,0.01,0]) # default: np.array([1,0.01,0])
        self.model.geom('floor').priority = 1 # >0

        self.root_states = torch.zeros((1,13))
        self.dof_pos = torch.zeros((1,self.n_ctrl))
        self.dof_vel = torch.zeros((1,self.n_ctrl))
        self.rigid_body_pos = torch.zeros((1,self.n_body-1,3))
        self.key_body_pos = torch.zeros((1,self._key_body_ids.shape[0],3))
        self.tick = torch.tensor(self.tick)

        self.obs = self._compute_humanoid_obs()
        self.obses = torch.zeros((self.horizon_length,self.obs.shape[-1]), device=self.device)
        self.raw_obses = torch.zeros_like(self.obses, device=self.device)
        self.raw_values = torch.zeros_like(self.obses, device=self.device)
        # timeouts = torch.zeros((self.horizon_length,), device=self.device) #[]
        self.resets = torch.zeros((self.horizon_length,), device=self.device) #[]
        self.terminates = torch.zeros((self.horizon_length,), device=self.device) #[]
        # rewards = torch.zeros((self.horizon_length,), device=self.device) #[]
        self.amp_obses = torch.zeros((self.horizon_length,self.obs.shape[-1]), device=self.device) #[]
        self.prev_obs = torch.zeros((1,self.obs.shape[-1]))
        self.prev_amp_obs = torch.zeros_like(self.amp_obses[0])
        # for res_dict
        self.neglogpacs = torch.zeros((self.horizon_length,), device=self.device) #[]
        self.values = torch.zeros((self.horizon_length,), device=self.device)
        self.actions = torch.zeros((self.horizon_length,self.n_ctrl), device=self.device)
        self.mus = torch.zeros((self.horizon_length,self.n_ctrl), device=self.device)
        self.sigmas = torch.zeros((self.horizon_length,self.n_ctrl), device=self.device)
        self.force = torch.zeros((3,))
        self.res_dict = None

        if (self.VERBOSE):
            print("PID controller ready")
            print('Floor friction: ',self.model.geom('floor').friction)
            print('Floor priority: ',self.model.geom('floor').priority)
    

    # ______________________ AMP utils ______________________

    def reset_PID(self):
        self.PID.reset()
        if (self.VERBOSE):
            print("PID controller reset")

    def init_models(self,model,running_mean_std,value_mean_std):
        self._model=model
        self.running_mean_std=running_mean_std
        self.value_mean_std=value_mean_std

    def assign_vel(self,dq=None,joint_idxs=None):
        if dq is not None:
            if joint_idxs is not None:
                self.data.qvel[joint_idxs] = dq
            else:
                self.data.qvel = dq

    # def get_v_base(self):
    #     """
    #         Get body position
    #     """
    #     return self.data.qvel[0:3].copy()

    def get_ps(self):
        """
            Get x
        """
        return self.data.xpos[1:].copy()

    def get_qposes(self):
        """
            Get 
        """
        return self.data.qpos[self.rev_joint_idxs+6].copy()

    def get_qvels(self):
        """
            Get 
        """
        return self.data.qvel[self.rev_joint_idxs+5].copy()
    
    def generator_step(self, action, render_every=1):
        return
    
    def step_queue(self, ctrl, queue):
        """
            Step
        """
        if self.is_running:
            return
        
        print("queue value: ", r)

        self.is_running = True
        
        self.step(ctrl=ctrl)
        p, R = self.get_pR_body()
        queue.put_async({"position": p, "rotation": R})

        self.is_running = False

    def step(self,ctrl=None,ctrl_idxs=None,nstep=1,INCREASE_TICK=True):
        """
            Step with PD Controller
        """

        super().step(ctrl=ctrl,ctrl_idxs=ctrl_idxs,nstep=nstep,INCREASE_TICK=INCREASE_TICK)

        # actor_root_states - position([0:3]), rotation([3:7]), linear velocity([7:10]), and angular velocity([10:13])

        result_dict = {
            "actor_root_states" : np.concatenate((self.get_p_body('base'), r2quat(self.get_R_body('base'))[[1,2,3,0]], self.get_qvel_joint('base')[0:3], self.get_qvel_joint('base')[3:6]), axis=-1),
            "dof_pos": self.get_qposes(),
            "dof_vel": self.get_qvels(),
            "rigid_body_pos": self.get_ps(),
            "contact_info": self.get_contact_info()
            # "root_p": self.get_p_body('base'),
            # "root_R": self.get_R_body('base')
        }


        return result_dict

    def get_objects_poses(self):
        # get objects
        obj_names = [body_name for body_name in self.body_names
            if body_name is not None and (body_name.startswith("obj_"))]
        n_obj = len(obj_names)

        # Place objects
        colors = np.array([plt.cm.gist_rainbow(x) for x in np.linspace(0,1,n_obj)])
        colors[:,3] = 1.0 # transparent objects
        obj_poses = np.empty((0, 7))
        for obj_idx,obj_name in enumerate(obj_names):

            geomadr = self.model.body(obj_name).geomadr[0]
            self.model.geom(geomadr).rgba = colors[obj_idx] # color

            jntadr  = self.model.body(obj_name).jntadr[0]
            qposadr = self.model.jnt_qposadr[jntadr]
            
            obj_poses = np.append(obj_poses, np.expand_dims(self.data.qpos[qposadr:qposadr+7], axis=0), axis=0)

        return obj_poses

    def pd_step(self,trgt=None,ctrl_idxs=None,nstep=1,INCREASE_TICK=True, SAVE_VID=True):
        """
            Step with PD Controller
        """
        qpos = self.get_q(self.ctrl_joint_idxs)
        self.PID.update(x_trgt=trgt,t_curr=self.get_sim_time(),x_curr=qpos,VERBOSE=self.VERBOSE)
        torque = self.PID.out()
        super().step(ctrl=torque,ctrl_idxs=ctrl_idxs,nstep=nstep,INCREASE_TICK=INCREASE_TICK)

        # actor_root_states = position([0:3]), rotation([3:7]), linear velocity([7:10]), and angular velocity([10:13]).
        result_dict = {
            "actor_root_states" : np.concatenate((self.get_p_body('base'), r2quat(self.get_R_body('base'))[[1,2,3,0]], self.get_qvel_joint('base')[0:3], self.get_qvel_joint('base')[3:6]), axis=-1),
            "dof_pos": self.get_qposes(),
            "dof_vel": self.get_qvels(),
            "rigid_body_pos": self.get_ps(),
            "contact_info": self.get_contact_info(),
            # "obj_poses": obj_poses
            # "root_p": self.get_p_body('base'),
            # "root_R": self.get_R_body('base')
        }
            
        return result_dict
    
    def _compute_humanoid_obs(self):
        from amp.tasks.amp.common_rig_amp_base import compute_humanoid_observations

        self.root_states[0] = torch.from_numpy(np.concatenate((self.get_p_body('base'), r2quat(self.get_R_body('base'))[[1,2,3,0]], self.get_qvel_joint('base')[0:3], self.get_qvel_joint('base')[3:6]), axis=-1))
        self.dof_pos[0] = torch.from_numpy(self.get_qposes())
        self.dof_vel[0] = torch.from_numpy(self.get_qvels())
        self.key_body_pos[0] = torch.from_numpy(self.get_ps()[self._key_body_ids, :])

        obs = compute_humanoid_observations(self.root_states, self.dof_pos, self.dof_vel,
                                        self.key_body_pos, False)
            
        
        return obs
    
    def _preproc_obs(self, obs_batch, running_mean_std):
        if type(obs_batch) is dict:
            for k,v in obs_batch.items():
                obs_batch[k] = self._preproc_obs(v)
        else:
            if obs_batch.dtype == torch.uint8:
                obs_batch = obs_batch.float() / 255.0
        if True: # TODO
            obs_batch = running_mean_std(obs_batch)
        return obs_batch

    # def pd_step_loop(self,model,running_mean_std,value_mean_std,ctrl_idxs=None,nstep=1,INCREASE_TICK=True):
    def pd_step_loop(self,ray_dict,ctrl_idxs=None,nstep=1,INCREASE_TICK=True):
        # from amp.tasks.amp.common_rig_amp_base import compute_humanoid_reward
        from amp.tasks.common_rig_amp import build_amp_observations
        from amp.tasks.amp.common_rig_amp_base import compute_humanoid_reset2
        
        # yoon0-0 TODO: reward tuning
        # @torch.jit.script
        # def compute_humanoid_reward(cur_root_state, pre_root_state):
        #     # type: (Tensor, Tensor) -> Tensor
        #     reward = torch.ones_like(cur_root_state[0])
        #     return reward

        # self._model,self.running_mean_std,self.value_mean_std = ray_dict['model'],ray_dict['running_mean_std'],ray_dict['value_mean_std']
        self._model.load_state_dict(ray_dict['model'])
        self.running_mean_std.load_state_dict(ray_dict['running_mean_std'])
        self.value_mean_std.load_state_dict(ray_dict['value_mean_std'])

        # actor_root_states = torch.zeros((self.horizon_length,13), device=self.device) #[]
        # dof_poses = torch.zeros((self.horizon_length,self.n_ctrl), device=self.device) #[]
        # dof_vels = torch.zeros((self.horizon_length,self.n_ctrl), device=self.device) #[]
        # rigid_body_poses = torch.zeros((self.horizon_length,self.n_body-1,3), device=self.device) #[]
        # contact_infos = []
        # contact_forces = torch.zeros((self.horizon_length,)+tuple(self._contact_forces.shape), device=self.device) #[]

        res_dicts = []
        i = 0 # TODO: i -> 0
        for n in range(self.horizon_length):
            
            # first prev obs
            if n == 0:
                self.root_states[0] = torch.from_numpy(np.concatenate((self.get_p_body('base'), r2quat(self.get_R_body('base'))[[1,2,3,0]], self.get_qvel_joint('base')[0:3], self.get_qvel_joint('base')[3:6]), axis=-1))
                self.dof_pos[0] = torch.from_numpy(self.get_qposes())
                self.dof_vel[0] = torch.from_numpy(self.get_qvels())
                self.rigid_body_pos[0] = torch.from_numpy(self.get_ps())
                self.key_body_pos[0] = self.rigid_body_pos[:, self._key_body_ids, :]

                self.prev_obs = self._compute_humanoid_obs()
                self.prev_amp_obs = build_amp_observations(self.root_states, self.dof_pos, self.dof_vel, self.key_body_pos, False)[0]

            # reset actor
            # self.obs, done_env_ids = self._env_reset_done()
            if self.reset_flag[0]:
                self.reset_PID()
                self.tick = torch.tensor(0)
                motion_ids = self._motion_lib.sample_motions(1)
                motion_times = self._motion_lib.sample_time(motion_ids)
                root_pos, root_rot, dof_pos, root_vel, root_ang_vel, dof_vel, key_pos \
                    = self._motion_lib.get_motion_state(motion_ids, motion_times)
                # root_pos[:,2] += 0.05
            
                # set env state
                # TODO: i
                q=torch.cat((root_pos[i], root_rot[i, [3,0,1,2]], dof_pos[i]),dim=0) # root_pos, root_rot, dof_pos
                dq=torch.cat((root_vel[i], root_ang_vel[i], dof_vel[i]),dim=0) # root_pos, root_rot, dof_pos
                self.assign_vel(dq=dq,joint_idxs=list(range(0,6))+self.ctrl_qvel_idxs)
                self.forward(q=q,joint_idxs=list(range(0,7))+self.ctrl_qpos_idxs,INCREASE_TICK=True)
                self.obs = self._compute_humanoid_obs()

            if self.USE_MUJOCO_VIEWER:
                self.render()

            # eval running mean std (updated outside ray worker after rollout)
            self.running_mean_std.eval()
            self.value_mean_std.eval()
            
            self.raw_obses[n] = self.obs.clone() # for updating running mean std 
            processed_obs = self._preproc_obs(self.obs, self.running_mean_std)
            input_dict = {
                'is_train': False,
                'prev_actions': None, 
                'obs' : processed_obs,
                'rnn_states' : None
            }
            with torch.no_grad():
                self.res_dict = self._model(input_dict)
            self.raw_values[n] = self.res_dict['values'].clone() # for updating running mean std 
            self.res_dict['values'] = self.value_mean_std(self.res_dict['values'], True)
            
            # PD control
            qpos = self.get_q(self.ctrl_joint_idxs)
            trgt = self.res_dict['actions']
            self.PID.update(x_trgt=trgt.cpu().numpy()[0],t_curr=self.get_sim_time(),x_curr=qpos,VERBOSE=self.VERBOSE)
            torque = self.PID.out()
            super().step(ctrl=torque,ctrl_idxs=ctrl_idxs,nstep=nstep,INCREASE_TICK=INCREASE_TICK)

            # next root state
            self.root_states[0] = torch.from_numpy(np.concatenate((self.get_p_body('base'), r2quat(self.get_R_body('base'))[[1,2,3,0]], self.get_qvel_joint('base')[0:3], self.get_qvel_joint('base')[3:6]), axis=-1))

            # contact forces
            contact_info = self.get_contact_info()
            for body_name, force in zip(set(contact_info[5]), np.unique(np.array(contact_info[1]), axis=0)): # NOTE : might occur error
                self.force = torch.from_numpy(force)
                body_id = self.model.body(body_name).id - 1
                self._contact_forces[body_id] = torch.abs(self.force)

            # timeout
            timeout = torch.where(torch.tensor(self.tick >= self.max_episode_length - 1), torch.ones((1,)), torch.zeros((1,)))

            # reset
            self.rigid_body_pos[0] = torch.from_numpy(self.get_ps())
            self.reset_flag, terminated = compute_humanoid_reset2(self.tick, self._contact_forces, self._contact_body_ids, self.rigid_body_pos, self.max_episode_length, True, 0.30) # TODO

            # TODO: reward
            # reward = compute_humanoid_reward(root_states.unsqueeze(dim=0), pre_root_states)

            # observation
            self.obs = self._compute_humanoid_obs()

            # amp observation
            self.dof_pos[0] = torch.from_numpy(self.get_qposes())
            self.dof_vel[0] = torch.from_numpy(self.get_qvels())
            self.key_body_pos[0] = self.rigid_body_pos[:, self._key_body_ids, :]
            amp_obs = build_amp_observations(self.root_states, self.dof_pos, self.dof_vel, self.key_body_pos, False)[0]

            # actor_root_states[n] = root_states# actor_root_states.append(root_states)
            # dof_poses[n] = dof_pos# dof_poses.append(dof_pos)
            # dof_vels[n] = dof_vel# dof_vels.append(dof_vel)
            # rigid_body_poses[n] = rigid_body_pos# rigid_body_poses.append(rigid_body_pos)
            # contact_forces[n] = self._contact_forces# contact_forces.append(self._contact_forces)
            # contact_infos.append(self.get_contact_info())
            self.obses[n] = self.obs # preprocessed
            # timeouts[n] = timeout# timeouts.append(timeout)
            self.resets[n] = self.reset_flag# resets.append(self.reset_flag)
            self.terminates[n] = terminated# terminates.append(terminated)
            # rewards[n] = reward# rewards.append(reward)
            self.amp_obses[n] = amp_obs# amp_obses.append(amp_obs)
            # res_dicts.append(res_dict)
            self.neglogpacs[n] = self.res_dict['neglogpacs']
            self.values[n] = self.res_dict['values']
            self.actions[n] = self.res_dict['actions']
            self.mus[n] = self.res_dict['mus']
            self.sigmas[n] = self.res_dict['sigmas']

        result_dict = {
            # "actor_root_states" : actor_root_states,
            # "pre_root_states": pre_root_states,
            # "dof_pos": dof_poses,
            # "dof_vel": dof_vels,
            # "rigid_body_pos": rigid_body_poses,
            # "contact_info": contact_infos,
            # "contact_forces": contact_forces,
            "obses": self.obses,
            # "timeouts": timeouts,
            "resets": self.resets,
            "terminates": self.terminates,
            # "rewards": rewards,
            "amp_obses": self.amp_obses,
            "neglogpacs": self.neglogpacs,
            "values": self.values,
            "actions": self.actions,
            "mus": self.mus,
            "sigmas": self.sigmas,
            # "res_dicts": res_dicts,
            "prev_obs": self.prev_obs,
            "prev_amp_obs": self.prev_amp_obs,
            "running_mean_std": self.running_mean_std,
            "value_mean_std": self.value_mean_std,
            "raw_obses": self.raw_obses,
            "raw_values": self.raw_values,
        }
        return result_dict# self.obses, self.resets, self.terminates, self.amp_obses, res_dicts, prev_obs, prev_amp_obs

    def render_queue(self, queue, render_every=1):
        """
            Render
        """
        if self.is_running:
            return
        r=queue.get()
        print("queue value: ", r)

        self.is_running = True
        self.init_viewer()

        self.render(render_every=render_every)

        time.sleep(10*r)
        print("test end")
        self.is_running = False
        self.viewer.close()
