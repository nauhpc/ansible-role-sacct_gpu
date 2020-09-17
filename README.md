# gpustats
Add gpu utilization stats to Slurm batch scheduler accounting db. Tested with CentOS-6.

## Background

This is intended to be used with [Slurm](https://slurm.schedmd.com/) to provide insight on job-gpu utilization. This adds short json-formatted string to 
sacct-database comment field containing stats for:

- number of used gpu's
- over job averate gpu utilization reported by nvidia-smi
- over job averate gpu memory utilization reported by nvidia-smi

## How it works

Basic idea is to run small code in the background that writes the stats every 1min. In Slurm's TaskEpilog (this is still when the db access for writing jobinfo is open) 
this information is collected per jobid and written to Comment-field of jobinfo in Slurm-Accounting-Database. 

## Deployment

On monsoon we have the gpustats.py code running on each node with a gpu. The
code will continually update a file unique to each job within /tmp/gpustats/ for
each job that uses a gpu. Our task_epilog.sh script sets the job comment to the
contents of this file, if it exists.
