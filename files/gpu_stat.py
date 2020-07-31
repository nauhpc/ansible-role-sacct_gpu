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

def read_shm(fil):
   jobinfo = {}
   if(os.path.exists(fil)):
      with open(fil) as fp:
         jobinfo=json.loads(fp.read())

   return jobinfo

def write_shm(jobinfo):
   with open('/tmp/gpustats.json', 'w') as fp:
      json.dump(jobinfo, fp)

def clean_gpustats(gpustats, max_age):
   keys_to_delete = []
   latest = datetime.now() - timedelta(days=max_age)
   latest = latest.strftime("%Y-%m-%d %H:%M:%S")
   for key in gpustats:
      timestamp = gpustats[key]['timestamp']
      if timestamp < latest:
         keys_to_delete.append(key)
   for key in keys_to_delete:
      del gpustats[key]
   return gpustats

def main():
   parser = argparse.ArgumentParser()
   parser.add_argument('-n', '--nosleep', help="Don't sleep at the beginning",
                       action="store_true")
   parser.add_argument('fname', nargs='?', default='/tmp/gpustats.json',
                       help='Name of JSON file for reading/storing data')
   parser.add_argument('-l', '--logfile',
                       help="Name of log file where any exceptions will be written to",
                       default='/tmp/gpustats.log')
   parser.add_argument('-m', '--max-age', type=int, default=7,
                       help='The maximum time (in days) for which the gpu stats of a job will be stored')
   args = parser.parse_args()

   logfile = open(args.logfile, 'a+')

   try:
      if not args.nosleep:
         time.sleep(random.randint(0, 30))

      # initialize stats
      current = {}
      jobs    = jobs_running()

      for job in jobs:
         current[job]={'gpu_util': 0, 'gpu_mem_max': 0, 'ngpu': 0,
                       'ncpu': 0, 'step': 1, 'gpu_power': 0 ,
                       'timestamp':
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

      # get current job info
      current = job_info(jobs, current)
      current = gpu_info(current)

      # combine with previous steps, calculate avgs and max
      prev = read_shm(args.fname)
      for job in jobs:
         if job in prev.keys():
            n = prev[job]['step']
            current[job]['gpu_util'] = ( float(prev[job]['gpu_util'])*n+float(current[job]['gpu_util']) )/(n+1)
            current[job]['gpu_power'] = ( float(prev[job]['gpu_power'])*n+float(current[job]['gpu_power']) )/(n+1)
            current[job]['gpu_mem_max']  = max(float(prev[job]['gpu_mem_max']), float(current[job]['gpu_mem_max']))
            current[job]['step'] = n+1

      for job in prev.keys():
         # if it's a job that is no longer running, save it's stats anyway
         if job not in jobs:
            current[job] = prev[job]

      # write json
      current = clean_gpustats(current, args.max_age)
      write_shm(current)

   except Exception as e:
      logfile.write(traceback.format_exc())

if __name__ == '__main__':
   main()
