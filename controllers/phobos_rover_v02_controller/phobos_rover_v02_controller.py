from controller import Robot, Camera, Motor
import sys
import math
import json
import toml
import zmq
import concurrent.futures
import numpy as np
from PIL import Image
import io
import time
import base64
import multiprocessing as mp
from multiprocessing import Pipe, Process

# Constants
str_motor_order = ['fl_gimbal', 'ml_gimbal', 'rl_gimbal', 'fr_gimbal', 'mr_gimbal', 'rr_gimbal']
drv_motor_order = ['fl_drive', 'ml_drive', 'rl_drive', 'fr_drive', 'mr_drive', 'rr_drive']
act_id_motor_group_index_map = {
    'DrvFL': 0,
	'DrvML': 1,
	'DrvRL': 2,
	'DrvFR': 3,
	'DrvMR': 4,
	'DrvRR': 5,
	'StrFL': 0,
	'StrML': 1,
	'StrRL': 2,
	'StrFR': 3,
	'StrMR': 4,
	'StrRR': 5,
	'ArmBase': 0,
	'ArmShoulder': 1,
	'ArmElbow': 2,
	'ArmWrist': 3,
	'ArmGrabber': 4
}

class PhobosRoverController(Robot):
    '''
    Controller interface for the phobos rover
    '''

    def __init__(self, params_path):
        '''
        Main constructor, initialises the controller.
        '''
        
        # Run the standard Robot class setup
        super(PhobosRoverController, self).__init__()

        # Load the params from the params path
        self.params = toml.load(params_path)

        # Setup equipment
        self.init_eqpt()

    def init_eqpt(self):
        '''
        Initialise the equipment of the rover.

        This function will find all equipment in the simulation and set 
        endpoints in self to be able to access them
        '''

        # Get camera endpoints
        self.cameras = {}
        self.cameras['LeftNav'] = self.getCamera('l_cam')
        self.cameras['RightNav'] = self.getCamera('r_cam')

        # Enable cameras at the specified frequencies
        self.cameras['LeftNav'].enable(self.params['left_nav_cam_timestep_ms'])
        self.cameras['RightNav'].enable(self.params['right_nav_cam_timestep_ms'])
        
        # Get and enable the depth images
        self.cameras['LeftDepth'] = self.getRangeFinder('l_depth')
        self.cameras['LeftDepth'].enable(self.params['left_depth_cam_timestep_ms'])

        # Get steer motors
        self.str_motors = [self.getMotor(name) for name in str_motor_order]

        # Get drive motors
        self.drv_motors = [self.getMotor(name) for name in drv_motor_order]

    def actuate_mech_dems(self, dems):
        '''
        Actuate the given mechanisms demands.

        Demands must have been validated already using `self.validate_mech_dems`.
        '''

        # Actuate position demands
        for act_id, position_rad in dems['pos_rad'].items():
            # Get the motor group
            group = act_id[:3]

            if group == 'Str':
                self.str_motors[act_id_motor_group_index_map[act_id]] \
                    .setPosition(position_rad)

        # Actuate speed demands
        for act_id, speed_rads in dems['speed_rads'].items():
            # Get motor group
            group = act_id[:3]

            if group == 'Drv':
                drv = self.drv_motors[act_id_motor_group_index_map[act_id]]
                drv.setPosition(float('inf'))
                drv.setVelocity(speed_rads)

    def stop(self):
        '''
        Bring the rover to a complete stop.
        '''
        for drv in self.drv_motors:
            drv.setVelocity(0.0)


def step(phobos):
    '''
    Step the rover simulation.

    Returns true if the controller should keep running.
    '''
    return phobos.step(phobos.params['controller_timestep_ms']) != -1

def run(phobos):
    '''
    Run the controller.
    '''

    # Create the cam server bg process and queue
    (cam_pipe, cam_child_pipe) = Pipe()
    cam_proc = Process(target=cam_process, args=(
        phobos.params['cam_rep_endpoint'], cam_child_pipe, 
    ))
    cam_proc.start()
    
    print('CamServer started')

    # Create zmq context
    context = zmq.Context()

    # Open mechanisms server
    mech_rep = context.socket(zmq.REP)
    mech_rep.bind(phobos.params['mech_rep_endpoint'])
    mech_pub = context.socket(zmq.PUB)
    mech_pub.bind(phobos.params['mech_pub_endpoint'])

    print('MechServer started')


    # Run flag
    run_controller = True

    print('Starting main control loop')
    while run_controller:
        # Run mechanisms task
        run_controller = handle_mech(phobos, mech_rep, mech_pub)

        # Handle any request from the camera process
        handle_cam_req(phobos, cam_pipe)

        # Step the rover
        run_controller = step(phobos)

        sys.stdout.flush()

    # Send stop to cam process
    cam_pipe.send('STOP')
    # Join the cam process
    cam_proc.join()
        

def handle_mech(phobos, mech_rep, mech_pub):
    '''
    Handle mechanisms commands and publish mech data
    '''
    # Flag indicating whether or not to stop the rover
    stop = False

    # Get mechanisms demands from the rep socket
    try: 
        dems_str = mech_rep.recv_string(flags=zmq.NOBLOCK)
        mech_dems = json.loads(dems_str)
    except zmq.Again:
        mech_dems = None
    except zmq.ZMQError as e:
        print(f'MechServer: Error - {e}, ({e.errno}')
        stop = True
        mech_dems = None
    except Exception as e:
        print(f'MechServer Exception: {e}')
        stop = True
        mech_dems = None

    # If no demand
    if mech_dems is None:
        # If an error occured stop the rover
        if stop:
            phobos.stop()
    else:
        # TODO: vaidate demands

        # Send response to client
        mech_rep.send_string('"DemsOk"')

        # print(f'MechDems: {mech_dems}')

        # Actuate
        phobos.actuate_mech_dems(mech_dems)

    return True

def handle_cam_req(phobos, cam_pipe):
    '''
    Handle a possible camera request from the camera process, then send data 
    back to the cam process for sending.
    '''

    cam_req = None

    # Poll for data from the camera process
    if cam_pipe.poll():

        cam_req = cam_pipe.recv()
    
    # If no request return now
    else:
        return

    # Data to send back to cam process
    cam_data = {}
    cam_data['format'] = cam_req['format']

    # For each camera in the request acquire an image
    for cam_id in cam_req['cameras']:
        # Get the raw data
        cam_data[cam_id] = {}
        cam_data[cam_id]['raw'] = phobos.cameras[cam_id].getImage();
        cam_data[cam_id]['timestamp'] = int(round(time.time() * 1000))
        cam_data[cam_id]['height'] = phobos.cameras[cam_id].getHeight()
        cam_data[cam_id]['width'] = phobos.cameras[cam_id].getWidth()

    # Send data to cam process
    cam_pipe.send(cam_data)

def handle_cam_send(cam_rep, cam_data):
    '''
    Send data via the zmq socket to the client, formatting in the correct way.
    '''

    # print('Building camera response')

    res = {}

    # iterate over the raw data and cam IDs
    for cam_id, data in cam_data.items():
        if cam_id == 'format':
            continue

        print(f'Processing {cam_id}')
        res[cam_id] = {};

        # Convert the raw data into a numpy array
        np_array = np.frombuffer(data['raw'], np.uint8)\
            .reshape((data['height'], data['width'], 4))

        # Rearrange from BRGA to RGBA
        np_array = np_array[...,[2,1,0,3]]

        # Convert to a PIL image
        image = Image.fromarray(np_array)

        # Create a byte array to write into
        img_bytes = io.BytesIO()

        # Save the image into this array
        if isinstance(cam_data['format'], str):
            image.save(img_bytes, format=cam_data['format'])
        else:
            image.save(img_bytes, format=list(cam_data['format'].keys())[0])

        # Get the raw byte value out
        img_bytes = img_bytes.getvalue()

        res[cam_id]['timestamp'] = data['timestamp']
        res[cam_id]['format'] = cam_data['format']
        res[cam_id]['b64_data'] = base64.b64encode(img_bytes).decode('ascii')


    # Get the response as a JSON string
    res_str = json.dumps(res)

    # print(f'Sending camera response ({len(res_str)} long)')

    # Send the string to the client
    cam_rep.send_string(res_str)

def cam_process(cam_endpoint, cam_pipe):
    '''
    Handle camera-related networking in a separate process.
    '''
    print('Starting camera process')

    # Create new zmq context
    context = zmq.Context()

    # Open camera server
    cam_rep = context.socket(zmq.REP)
    cam_rep.bind(cam_endpoint)

    # Flag keeping the process running
    run_process = True

    # Flag indicating if we're still handling a request
    handling_req = False

    while run_process:

        msg = None

        # First poll the pipe to see if there's any data
        if cam_pipe.poll():
            # Recieve message from main process
            msg = cam_pipe.recv()

            # If got a stop message exit the loop
            if isinstance(msg, str):
                if msg == 'STOP':
                    break
        else:
            # If no data, pass
            pass

        # If we got data from the main process send it
        if isinstance(msg, dict):
            handle_cam_send(cam_rep, msg)

            # Unset the handling req flag
            handling_req = False

        # If still handling a request don't try to recieve data form the client
        if handling_req:
            continue

        # Get request from the rep socket
        try:
            req_str = cam_rep.recv_string(flags=zmq.NOBLOCK)
            cam_req = json.loads(req_str)
        except zmq.Again:
            cam_req = None
        except zmq.ZMQError as e:
            print(f'CamServer: Error - {e}, ({e.errno}')
            cam_req = None
        except Exception as e:
            print(f'CamServer Exception: {e}')
            cam_req = None

        if cam_req is None:
            continue

        # Send request to main process
        cam_pipe.send(cam_req)

        # Raise handling req flag
        handling_req = True

    # Close the pipe
    cam_pipe.close()


def main():

    # Create phobos and run controller
    phobos = PhobosRoverController('../../params/phobos_rover_v02_controller.toml')

    run(phobos)

if __name__ == '__main__':

    main()