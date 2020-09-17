#!/usr/bin/python3

# author mhakala
import json
import re
import subprocess
import tempfile
import os
import xml.etree.cElementTree as ET
import argparse
import os.path
import time
import random
from datetime import datetime
from datetime import timedelta
import traceback
import configparser
import glob

def jobs_running():
    """find slurm-job-ids active on this node"""
    data = subprocess.check_output(['squeue', '-w', os.uname()[1].split('.')[0], '-h', '-o', '%A']).decode()
    return data.split()

def pid2id(pid):
    """convert pid to slurm jobid"""
    with open('/proc/%s/cgroup' % pid) as f:
        for line in f:
            m = re.search('.*slurm\/uid_.*\/job_(\d+)\/.*', line)
            if m:
                return m.group(1)
    return None

# get needed slurm values for each running job on the node
def job_info(jobs,current):
    for job in jobs:
        output = subprocess.check_output(['scontrol', '-o', 'show', 'job', job]).decode()
        cpus   = re.search('NumCPUs=(\d+)', output)
        tres   = re.search('TRES=(\S+)', output).group(1)
        nodes  = re.search('NumNodes=(\d+)', output)

        ngpu = 0
        for g in tres.split(','):
            gs = g.split('=')
            if gs[0] == 'gres/gpu:tesla':
                if len(gs) == 1:
                    ngpu = 1
                else:
                    ngpu = int(gs[-1])

        # drop multi-node jobs (will be added later if needed)
        if int(nodes.group(1)) > 1:
            del current[job]
        else:
            current[job]['ngpu'] = ngpu
            current[job]['ncpu']=int(cpus.group(1))

    return current


def gpu_info(jobinfo):

    output = subprocess.check_output(['nvidia-smi', '-q', '-x']).decode()
    root = ET.fromstring(output)

    for gpu in root.findall('gpu'):
        procs = gpu.find('processes')
        mtot = 0.
        jobid = None
        # Here we assume that multiple job id's cannot access the same
        # GPU
        for pi in procs.findall('process_info'):
            pid = pi.find('pid').text
            jobid = pid2id(pid)
            # Assume used_memory is of the form '1750 MiB'. Needs fixing
            # if the unit is anything but MiB.
            mtot += float(pi.find('used_memory').text.split()[0])

        util = gpu.find('utilization')
        # Here assume gpu utilization is of the form
        # '100 %'
        gutil = float(util.find('gpu_util').text.split()[0])

        # power_draw is of the form 35.25 W
        power = gpu.find('power_readings')
        gpwrdraw = float(power.find('power_draw').text.split()[0])

        # only update, if jobid not dropped (multinode jobs)
        # if a job is using multiple GPUs, code below should execute again
        if jobid in jobinfo.keys():
            if jobinfo[jobid]['ngpu'] != 0:
                jobinfo[jobid]['gpu_util'] += gutil/jobinfo[jobid]['ngpu']
                jobinfo[jobid]['gpu_power'] += gpwrdraw
                jobinfo[jobid]['gpu_mem_max'] = max(mtot,
                                                    jobinfo[jobid]['gpu_mem_max'])
    return jobinfo

def read_shm(dir_name):
    jobinfo = {}
    for fpath in glob.glob(dir_name + '*.json'):
        jobid = fpath.replace(dir_name, '').replace('.json', '')
        with open(fpath, 'r') as fp:
            jobinfo[jobid] = json.loads(fp.read())
    return jobinfo

def write_shm(jobinfo, running_jobids, dir_path, max_age):
    latest = datetime.now() - timedelta(days=max_age)
    latest = latest.strftime("%Y-%m-%d %H:%M:%S")

    for jobid in jobinfo:
        fpath = dir_path + str(jobid) + '.json'
        if jobid in running_jobids and jobinfo[jobid]['ngpu'] != 0:
            with open(fpath, 'w') as fp:
                json.dump(jobinfo[jobid], fp)
        elif jobinfo[jobid]['timestamp'] < latest:
            os.remove(fpath)

def dir_path(path):
    if os.path.isdir(path):
        return path
    else:
        raise argparse.ArgumentTypeError("readable_dir:" +
                                         str(path) +
                                         " is not a valid path")

def main():
    start_time = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument('dir_path',
                        type=dir_path,
                        nargs='?',
                        default='/tmp/gpu_stats/',
                        help="The directory where a JSON for each job is stored")
    parser.add_argument('-n', '--nosleep',
                        help="Don't sleep at the beginning",
                        action="store_true")    
    parser.add_argument('-l',
                        '--logfile',
                        help="Name of log file where any exceptions will be written to",
                        default='/tmp/gpustats.log')
    parser.add_argument('-m',
                        '--max-age',
                        type=int,
                        default=1,
                        help='The maximum time (in days) for which the gpu stats of a job will be stored')
    args = parser.parse_args()

    if args.dir_path[-1] != '/':
        args.dir_path += '/'

    logfile = open(args.logfile, 'a+')

    try:
        if not args.nosleep:
            time.sleep(random.randint(0, 30))

        # initialize stats
        current = {}
        jobs    = jobs_running()

        for job in jobs:
            current[job]={'gpu_util': 0, 'gpu_mem_max': 0, 'ngpu': 0,
                          'ncpu': 0, 'step': 1, 'gpu_power': 0,
                          'timestamp':
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        # get current job info
        current = job_info(jobs, current)
        current = gpu_info(current)

        # running_jobids contains jobids of jobs that are running
        # if a jobid is not in this set,
        # then we don't need to write to the corresponding file
        running_jobids = set(current.keys())

        # combine with previous steps, calculate avgs and max
        prev = read_shm(args.dir_path)

        for job in jobs:
            if job in prev.keys():
                n = prev[job]['step']
                current[job]['gpu_util'] = ( float(prev[job]['gpu_util'])*n+float(current[job]['gpu_util']) )/(n+1)
                current[job]['gpu_power'] = ( float(prev[job]['gpu_power'])*n+float(current[job]['gpu_power']) )/(n+1)
                current[job]['gpu_mem_max']  = max(float(prev[job]['gpu_mem_max']), float(current[job]['gpu_mem_max']))
                current[job]['step'] = n+1

        for job in prev.keys():
            if job not in jobs:
                # it must be a job that is no longer running
                current[job] = prev[job]

        # write json
        write_shm(current, running_jobids, args.dir_path, args.max_age)

    except Exception as e:
        logfile.write(traceback.format_exc())

    end_time = time.time()
    if end_time - start_time > 55.0:
        logfile.write("WARNING: runtime was longer than expected at " +
                      str(end_time - start_time) +
                      " seconds\n")

if __name__ == '__main__':
    main()
