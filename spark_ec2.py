#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import with_statement

import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib2
import json
from optparse import OptionParser
from sys import stderr
import boto
from boto.ec2.blockdevicemapping import BlockDeviceMapping, EBSBlockDeviceType
from boto import ec2

# A static URL from which to figure out the latest Mesos EC2 AMI
LATEST_AMI_URL = "http://s3.amazonaws.com/ampcamp-amis/latest-ampcamp3"


# Configure and parse our command-line arguments
def parse_args():
  parser = OptionParser(usage="spark-ec2 [options] <action> <cluster_name>"
      + "\n\n<action> can be: launch, destroy, login, stop, start, get-master",
      add_help_option=False)
  parser.add_option("-h", "--help", action="help",
                    help="Show this help message and exit")
  parser.add_option("-s", "--slaves", type="int", default=5,
      help="Number of slaves to launch (default: 1)")
  parser.add_option("-w", "--wait", type="int", default=120,
      help="Seconds to wait for nodes to start (default: 120)")
  parser.add_option("-k", "--key-pair",
      help="Key pair to use on instances")
  parser.add_option("-i", "--identity-file",
      help="SSH private key file to use for logging into instances")
  parser.add_option("-t", "--instance-type", default="m1.xlarge",
      help="Type of instance to launch (default: m1.large). " +
           "WARNING: must be 64-bit; small instances won't work")
  parser.add_option("-m", "--master-instance-type", default="",
      help="Master instance type (leave empty for same as instance-type)")
  parser.add_option("-r", "--region", default="us-east-1",
      help="EC2 region zone to launch instances in")
  parser.add_option("-z", "--zone", default="",
      help="Availability zone to launch instances in, or 'all' to spread " +
           "slaves across multiple (an additional $0.01/Gb for bandwidth" +
           "between zones applies)")
  parser.add_option("-a", "--ami", default="latest",
      help="Amazon Machine Image ID to use, or 'latest' to use latest " +
           "available AMI (default: latest)")
  parser.add_option("-D", metavar="[ADDRESS:]PORT", dest="proxy_port",
      help="Use SSH dynamic port forwarding to create a SOCKS proxy at " +
            "the given local address (for use with login)")
  parser.add_option("--resume", action="store_true", default=False,
      help="Resume installation on a previously launched cluster " +
           "(for debugging)")
  parser.add_option("--ebs-vol-size", metavar="SIZE", type="int", default=0,
      help="Attach a new EBS volume of size SIZE (in GB) to each node as " +
           "/vol. The volumes will be deleted when the instances terminate. " +
           "Only possible on EBS-backed AMIs.")
  parser.add_option("--swap", metavar="SWAP", type="int", default=1024,
      help="Swap space to set up per node, in MB (default: 1024)")
  parser.add_option("--spot-price", metavar="PRICE", type="float",
      help="If specified, launch slaves as spot instances with the given " +
            "maximum price (in dollars)")
  parser.add_option("-g", "--ganglia", action="store_true", default=True,
      help="Setup ganglia monitoring for the cluster. NOTE: The ganglia " +
      "monitoring page will be publicly accessible")
  parser.add_option("-u", "--user", default="root",
      help="The ssh user you want to connect as (default: root)")
  parser.add_option("--delete-groups", action="store_true", default=False,
      help="When destroying a cluster, also destroy the security groups that were created")

  parser.add_option("--copy", action="store_true", default=False,
      help="Copy AMP Camp data from S3 to ephemeral HDFS after " +
           "launching the cluster (default: false)")

  parser.add_option("--s3-stats-bucket", default="default",
      help="S3 bucket to copy ampcamp data  from (default: ampcamp-data/wikistats_20090505-01)")

  parser.add_option("--s3-small-bucket", default="default",
      help="S3 bucket to copy ampcamp restricted data from " +
           "(default: ampcamp-data/wikistats_20090505_restricted-01)")

  parser.add_option("--s3-features-bucket", default="default",
      help="S3 bucket to copy ampcamp features data from " +
            "(default: ampcamp-data/wikistats_featurized-01)")

  parser.add_option("--destroy-noprompt", action="store_true", default=False,
      help="Disable prompt confirmation for destroy. ONLY do this if you " +
           "are sure you want to destroy a cluster (default: false)")


  (opts, args) = parser.parse_args()
  if len(args) != 2:
    parser.print_help()
    sys.exit(1)
  (action, cluster_name) = args
  if opts.identity_file == None and action in ['launch', 'login', 'start']:
    print >> stderr, ("ERROR: The -i or --identity-file argument is " +
                      "required for " + action)
    sys.exit(1)

  # Boto config check
  # http://boto.cloudhackers.com/en/latest/boto_config_tut.html
  home_dir = os.getenv('HOME')
  if home_dir == None or not os.path.isfile(home_dir + '/.boto'):
    if not os.path.isfile('/etc/boto.cfg'):
      if os.getenv('AWS_ACCESS_KEY_ID') == None:
        print >> stderr, ("ERROR: The environment variable AWS_ACCESS_KEY_ID " +
                          "must be set")
        sys.exit(1)
      if os.getenv('AWS_SECRET_ACCESS_KEY') == None:
        print >> stderr, ("ERROR: The environment variable AWS_SECRET_ACCESS_KEY " +
                          "must be set")
        sys.exit(1)
  return (opts, action, cluster_name)


# Get the EC2 security group of the given name, creating it if it doesn't exist
def get_or_make_group(conn, name):
  groups = conn.get_all_security_groups()
  group = [g for g in groups if g.name == name]
  if len(group) > 0:
    return group[0]
  else:
    print "Creating security group " + name
    return conn.create_security_group(name, "Spark EC2 group")


# Wait for a set of launched instances to exit the "pending" state
# (i.e. either to start running or to fail and be terminated)
def wait_for_instances(conn, instances):
  while True:
    for i in instances:
      i.update()
    if len([i for i in instances if i.state == 'pending']) > 0:
      time.sleep(5)
    else:
      return


# Check whether a given EC2 instance object is in a state we consider active,
# i.e. not terminating or terminated. We count both stopping and stopped as
# active since we can restart stopped clusters.
def is_active(instance):
  return (instance.state in ['pending', 'running', 'stopping', 'stopped'])


# Launch a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master, slave
# and zookeeper instances (in that order).
# Fails if there already instances running in the cluster's groups.
def launch_cluster(conn, opts, cluster_name):
  print "Setting up security groups..."
  master_group = get_or_make_group(conn, "ampcamp3-master")
  slave_group = get_or_make_group(conn, "ampcamp3-slaves")
  zoo_group = get_or_make_group(conn, "ampcamp3-zoo")
  if master_group.rules == []: # Group was just now created
    master_group.authorize(src_group=master_group)
    master_group.authorize(src_group=slave_group)
    master_group.authorize(src_group=zoo_group)
    master_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    master_group.authorize('tcp', 8080, 8081, '0.0.0.0/0')
    master_group.authorize('tcp', 50030, 50030, '0.0.0.0/0')
    master_group.authorize('tcp', 50070, 50070, '0.0.0.0/0')
    master_group.authorize('tcp', 60070, 60070, '0.0.0.0/0')
    master_group.authorize('tcp', 33000, 33010, '0.0.0.0/0')
    master_group.authorize('tcp', 3030, 3040, '0.0.0.0/0')
    master_group.authorize('tcp', 5050, 5050, '0.0.0.0/0')
    master_group.authorize('tcp', 38090, 38090, '0.0.0.0/0')
    if opts.ganglia:
      master_group.authorize('tcp', 5080, 5080, '0.0.0.0/0')
  if slave_group.rules == []: # Group was just now created
    slave_group.authorize(src_group=master_group)
    slave_group.authorize(src_group=slave_group)
    slave_group.authorize(src_group=zoo_group)
    slave_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    slave_group.authorize('tcp', 8080, 8081, '0.0.0.0/0')
    slave_group.authorize('tcp', 50060, 50060, '0.0.0.0/0')
    slave_group.authorize('tcp', 50075, 50075, '0.0.0.0/0')
    slave_group.authorize('tcp', 60060, 60060, '0.0.0.0/0')
    slave_group.authorize('tcp', 60075, 60075, '0.0.0.0/0')
    slave_group.authorize('tcp', 5051, 5051, '0.0.0.0/0')
  if zoo_group.rules == []: # Group was just now created
    zoo_group.authorize(src_group=master_group)
    zoo_group.authorize(src_group=slave_group)
    zoo_group.authorize(src_group=zoo_group)
    zoo_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    zoo_group.authorize('tcp', 2181, 2181, '0.0.0.0/0')
    zoo_group.authorize('tcp', 2888, 2888, '0.0.0.0/0')
    zoo_group.authorize('tcp', 3888, 3888, '0.0.0.0/0')

  # Check if instances are already running in our groups
  active_nodes = get_existing_cluster(conn, opts, cluster_name,
                                      die_on_error=False)
  if any(active_nodes):
    print >> stderr, ("ERROR: There are already instances running in " +
        "group %s, %s or %s" % (master_group.name, slave_group.name, zoo_group.name))
    sys.exit(1)

  # Figure out the latest AMI from our static URL
  if opts.ami == "latest":
    try:
      opts.ami = urllib2.urlopen(LATEST_AMI_URL).read().strip()
      print "Latest Spark AMI: " + opts.ami
    except:
      print >> stderr, "Could not read " + LATEST_AMI_URL
      sys.exit(1)

  print "Launching instances..."

  try:
    image = conn.get_all_images(image_ids=[opts.ami])[0]
  except:
    print >> stderr, "Could not find AMI " + opts.ami
    sys.exit(1)

  # Create block device mapping so that we can add an EBS volume if asked to
  block_map = BlockDeviceMapping()
  if opts.ebs_vol_size > 0:
    device = EBSBlockDeviceType()
    device.size = opts.ebs_vol_size
    device.delete_on_termination = True
    block_map["/dev/sdv"] = device

  # Launch slaves
  if opts.spot_price != None:
    # Launch spot instances with the requested price
    print ("Requesting %d slaves as spot instances with price $%.3f" %
           (opts.slaves, opts.spot_price))
    zones = get_zones(conn, opts)
    num_zones = len(zones)
    i = 0
    my_req_ids = []
    for zone in zones:
      num_slaves_this_zone = get_partition(opts.slaves, num_zones, i)
      slave_reqs = conn.request_spot_instances(
          price = opts.spot_price,
          image_id = opts.ami,
          launch_group = "launch-group-%s" % cluster_name,
          placement = zone,
          count = num_slaves_this_zone,
          key_name = opts.key_pair,
          security_groups = [slave_group],
          instance_type = opts.instance_type,
          block_device_map = block_map)
      my_req_ids += [req.id for req in slave_reqs]
      i += 1

    print "Waiting for spot instances to be granted..."
    try:
      while True:
        time.sleep(10)
        reqs = conn.get_all_spot_instance_requests()
        id_to_req = {}
        for r in reqs:
          id_to_req[r.id] = r
        active_instance_ids = []
        for i in my_req_ids:
          if i in id_to_req and id_to_req[i].state == "active":
            active_instance_ids.append(id_to_req[i].instance_id)
        if len(active_instance_ids) == opts.slaves:
          print "All %d slaves granted" % opts.slaves
          reservations = conn.get_all_instances(active_instance_ids)
          slave_nodes = []
          for r in reservations:
            slave_nodes += r.instances
          break
        else:
          print "%d of %d slaves granted, waiting longer" % (
            len(active_instance_ids), opts.slaves)
    except:
      print "Canceling spot instance requests"
      conn.cancel_spot_instance_requests(my_req_ids)
      # Log a warning if any of these requests actually launched instances:
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name, die_on_error=False)
      running = len(master_nodes) + len(slave_nodes) + len(zoo_nodes)
      if running:
        print >> stderr, ("WARNING: %d instances are still running" % running)
      sys.exit(0)
  else:
    # Launch non-spot instances
    zones = get_zones(conn, opts)
    num_zones = len(zones)
    i = 0
    slave_nodes = []
    for zone in zones:
      num_slaves_this_zone = get_partition(opts.slaves, num_zones, i)
      if num_slaves_this_zone > 0:
        slave_res = image.run(key_name = opts.key_pair,
                              security_groups = [slave_group],
                              instance_type = opts.instance_type,
                              placement = zone,
                              min_count = num_slaves_this_zone,
                              max_count = num_slaves_this_zone,
                              block_device_map = block_map)
        slave_nodes += slave_res.instances
        print "Launched %d slaves in %s, regid = %s" % (num_slaves_this_zone,
                                                        zone, slave_res.id)
      i += 1

  # Launch masters
  master_type = opts.master_instance_type
  if master_type == "":
    master_type = opts.instance_type
  if opts.zone == 'all':
    opts.zone = random.choice(conn.get_all_zones()).name
  master_res = image.run(key_name = opts.key_pair,
                         security_groups = [master_group],
                         instance_type = master_type,
                         placement = opts.zone,
                         min_count = 1,
                         max_count = 1,
                         block_device_map = block_map)
  master_nodes = master_res.instances
  print "Launched master in %s, regid = %s" % (zone, master_res.id)

  time.sleep(20)

  # Create the right tags
  tags = {}
  tags['cluster'] = cluster_name

  tags['type'] = 'slave'
  for node in slave_nodes:
    conn.create_tags([node.id], tags)

  tags['type'] = 'master'
  for node in master_nodes:
    conn.create_tags([node.id], tags)

  zoo_nodes = []

  # Return all the instances
  return (master_nodes, slave_nodes, zoo_nodes)


# Get the EC2 instances in an existing cluster if available.
# Returns a tuple of lists of EC2 instance objects for the masters,
# slaves and zookeeper nodes (in that order).
def get_existing_cluster(conn, opts, cluster_name, die_on_error=True):
  print "Searching for existing cluster " + cluster_name + "..."
  reservations = conn.get_all_instances()
  master_nodes = []
  slave_nodes = []
  zoo_nodes = []
  for res in reservations:
    active = [i for i in res.instances if is_active(i)]
    if len(active) > 0:
      for instance in res.instances:
        if 'tags' in instance.__dict__ and 'cluster' in instance.tags \
          and 'type' in instance.tags:
            if instance.tags['cluster'] == cluster_name:
              if instance.tags['type'] == 'master':
                master_nodes.append(instance)
              elif instance.tags['type'] == 'slave':
                slave_nodes.append(instance)
              elif instance.tags['type'] == 'zoo':
                zoo_nodes.append(instance)

  if any((master_nodes, slave_nodes, zoo_nodes)):
    print ("Found %d master(s), %d slaves, %d ZooKeeper nodes" %
           (len(master_nodes), len(slave_nodes), len(zoo_nodes)))
  if (master_nodes != [] and slave_nodes != []) or not die_on_error:
    return (master_nodes, slave_nodes, zoo_nodes)
  else:
    if master_nodes == [] and slave_nodes != []:
      print "ERROR: Could not find master in group " + cluster_name + "-master"
    elif master_nodes != [] and slave_nodes == []:
      print "ERROR: Could not find slaves in group " + cluster_name + "-slaves"
    else:
      print "ERROR: Could not find any existing cluster"
    sys.exit(1)


# Deploy configuration files and run setup scripts on a newly launched
# or started EC2 cluster.
def setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, deploy_ssh_key):
  master = master_nodes[0].public_dns_name
  if deploy_ssh_key:
    print "Copying SSH key %s to master..." % opts.identity_file
    ssh(master, opts, 'mkdir -p ~/.ssh')
    scp(master, opts, opts.identity_file, '~/.ssh/id_rsa')
    ssh(master, opts, 'chmod 600 ~/.ssh/id_rsa')

  modules = ['ephemeral-hdfs', 'persistent-hdfs', 'mesos', 'spark-standalone', 'training']

  if opts.ganglia:
    modules.append('ganglia')

  # NOTE: We should clone the repository before running deploy_files to
  # prevent ec2-variables.sh from being overwritten
  ssh(master, opts,
      "rm -rf spark-ec2 && git clone -b ampcamp3 https://github.com/mesos/spark-ec2.git")

  print "Deploying files to master..."
  deploy_files(conn, "deploy.generic", opts, master_nodes, slave_nodes,
          zoo_nodes, modules)

  print "Running setup on master..."
  setup_spark_cluster(master, opts)
  print "Done!"

def setup_spark_cluster(master, opts):
  ssh(master, opts, "chmod u+x spark-ec2/setup.sh")
  ssh(master, opts, "spark-ec2/setup.sh")

def check_spark_json(spark_json, opts):
  json_data = json.loads(spark_json)
  ## Find number of cpus from status page
  got_num_cpus = int(json_data.get("cores"))

  ## Find expected number of CPUs
  num_cpus_per_slave = get_num_cpus(opts.instance_type)
  expected_num_cpus = num_cpus_per_slave * opts.slaves

  print "Spark master reports " + str(got_num_cpus) + " CPUs, expected " + str(expected_num_cpus)

  # TODO(shivaram): Should we check amount of memory as well ?

  if int(got_num_cpus) == int(expected_num_cpus):
    return 0
  else:
    return -1

def check_spark_cluster(master_nodes, opts):
  master = master_nodes[0].public_dns_name
  url = "http://" + master + ":8080/json"
  try:
    response = urllib2.urlopen(url)
    if response.code != 200:
      print "Spark master " + url + " returned " + str(response.code)
      return -1
    master_html = response.read()
    return check_spark_json(master_html, opts)
  except NameError as e:
    # If we get an exception, return error
    print "Exception in opening the url " + url
    return -1

def copy_ampcamp_data_from_ebs(master_nodes, opts):
  master = master_nodes[0].public_dns_name

  print "Copying AMP Camp Wikipedia pagecount data..."
  ssh(master, opts,
      "/root/ephemeral-hdfs/bin/hadoop fs -copyFromLocal /ampcamp-data/pagecounts /wiki/pagecounts")
  print "Copying AMP Camp Wikipedia featurized data..."
  ssh(master, opts,
      "/root/ephemeral-hdfs/bin/hadoop fs -copyFromLocal /ampcamp-data/wikistats_featurized /")
  print "Copying AMP Camp Wikipedia articles data..."
  ssh(master, opts,
      "/root/ephemeral-hdfs/bin/hadoop fs -copyFromLocal /ampcamp-data/enwiki_txt /")

def copy_ampcamp_data_from_s3(master_nodes, opts):
  master = master_nodes[0].public_dns_name

  ssh(master, opts, "/root/ephemeral-hdfs/bin/stop-mapred.sh")
  # Wait for mapred to stop
  time.sleep(10)

  ssh(master, opts, "/root/ephemeral-hdfs/bin/start-mapred.sh")
  # Wait for mapred to start
  time.sleep(10)

  (s3_access_key, s3_secret_key) = get_s3_keys()

  # Escape '/' in S3-keys
  # NOTE: This doesn't work as the code path from DistCp assumes path
  # strings are unescaped, but looks for '/' to separate URI components.
  #
  # If the access key has a slash it is best to put it in core-site.xml
  # and re-run copy-data
  # All other reserved characters work without being escaped
  #
  # s3_access_key = s3_access_key.replace('/', '%2F')
  # s3_secret_key = s3_secret_key.replace('/', '%2F')

  # ssh(master, opts, "/root/ephemeral-hdfs/bin/hadoop fs -rmr /wiki")

  if (opts.s3_stats_bucket == "default"):
    s3_buckets_range = range(1, 9)
    opts.s3_stats_bucket = "ampcamp-data/wikistats_20090505-0" + \
        str(random.choice(s3_buckets_range))

  if (opts.s3_small_bucket == "default"):
    s3_buckets_range = range(1, 9)
    opts.s3_small_bucket = "ampcamp-data/wikistats_20090505_restricted-0" + \
        str(random.choice(s3_buckets_range))

  if (opts.s3_features_bucket == "default"):
    s3_buckets_range = range(1, 9)
    opts.s3_features_bucket = "ampcamp-data/wikistats_featurized-0" + \
        str(random.choice(s3_buckets_range))

  set_s3_keys_in_hdfs(master, opts, s3_access_key, s3_secret_key)

  ssh(master, opts, "/root/ephemeral-hdfs/bin/hadoop distcp " +
                    "s3n://" + opts.s3_stats_bucket + " " +
                    "hdfs://`hostname`:9000/wiki/pagecounts")

  # ssh(master, opts, "/root/ephemeral-hdfs/bin/hadoop fs -rmr /wikistats_20090505-07_restricted")

  ssh(master, opts, "/root/ephemeral-hdfs/bin/hadoop distcp " +
                    "s3n://" + opts.s3_small_bucket + " " +
                    "hdfs://`hostname`:9000/wikistats_20090505-07_restricted")

  ssh(master, opts, "/root/ephemeral-hdfs/bin/hadoop distcp " +
                    "s3n://" + opts.s3_features_bucket + " " +
                    "hdfs://`hostname`:9000/wikistats_featurized")

def set_s3_keys_in_hdfs(master, opts, s3_access_key, s3_secret_key):
  ssh(master, opts, "cd ephemeral-hdfs/conf; sed -i \"s/\!-- p/p/g\" core-site.xml")
  ssh(master, opts, "cd ephemeral-hdfs/conf; sed -i \"s/y --/y/g\" core-site.xml")
  ssh(master, opts, "cd ephemeral-hdfs/conf; " + \
      "sed -i \"/fs.s3n.awsAccessKeyId/{N; s/value>.*<\/value/value>" + \
      s3_access_key.replace("/", "\/") + "<\/value/g }\" core-site.xml")
  ssh(master, opts, "cd ephemeral-hdfs/conf; " + \
      "sed -i \"/fs.s3n.awsSecretAccessKey/{N; s/value>.*<\/value/value>" + \
      s3_secret_key.replace("/", "\/") + "<\/value/g }\" core-site.xml")

def get_s3_keys():
  if os.getenv('S3_AWS_ACCESS_KEY_ID') != None:
    s3_access_key = os.getenv("S3_AWS_ACCESS_KEY_ID")
  else:
    s3_access_key = os.getenv("AWS_ACCESS_KEY_ID")
  if os.getenv('S3_AWS_SECRET_ACCESS_KEY') != None:
    s3_secret_key = os.getenv("S3_AWS_SECRET_ACCESS_KEY")
  else:
    s3_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
  return (s3_access_key, s3_secret_key)


# Wait for a whole cluster (masters, slaves and ZooKeeper) to start up
def wait_for_cluster(conn, wait_secs, master_nodes, slave_nodes, zoo_nodes):
  print "Waiting for instances to start up..."
  time.sleep(5)
  wait_for_instances(conn, master_nodes)
  wait_for_instances(conn, slave_nodes)
  if zoo_nodes != []:
    wait_for_instances(conn, zoo_nodes)
  print "Waiting %d more seconds..." % wait_secs
  time.sleep(wait_secs)


# Get number of local disks available for a given EC2 instance type.
def get_num_disks(instance_type):
  # From http://docs.amazonwebservices.com/AWSEC2/latest/UserGuide/index.html?InstanceStorage.html
  disks_by_instance = {
    "m1.small":    1,
    "m1.medium":   1,
    "m1.large":    2,
    "m1.xlarge":   4,
    "t1.micro":    1,
    "c1.medium":   1,
    "c1.xlarge":   4,
    "m2.xlarge":   1,
    "m2.2xlarge":  1,
    "m2.4xlarge":  2,
    "cc1.4xlarge": 2,
    "cc2.8xlarge": 4,
    "cg1.4xlarge": 2
  }
  if instance_type in disks_by_instance:
    return disks_by_instance[instance_type]
  else:
    print >> stderr, ("WARNING: Don't know number of disks on instance type %s; assuming 1"
                      % instance_type)
    return 1

# Get number of CPUs available for a given EC2 instance type.
def get_num_cpus(instance_type):
  # From http://aws.amazon.com/ec2/instance-types/
  cpus_by_instance = {
    "m1.small":    1,
    "m1.large":    2,
    "m1.xlarge":   4,
    "t1.micro":    1,
    "c1.medium":   2,
    "c1.xlarge":   8,
    "m2.xlarge":   2,
    "m2.2xlarge":  4,
    "m2.4xlarge":  8,
    "cc1.4xlarge": 8,
    "cc2.8xlarge": 16,
    "cg1.4xlarge": 8
  }
  if instance_type in cpus_by_instance:
    return cpus_by_instance[instance_type]
  else:
    print >> stderr, ("WARNING: Don't know number of cpus on instance type %s; assuming 2"
                      % instance_type)
    return 2

# Deploy the configuration file templates in a given local directory to
# a cluster, filling in any template parameters with information about the
# cluster (e.g. lists of masters and slaves). Files are only deployed to
# the first master instance in the cluster, and we expect the setup
# script to be run on that instance to copy them to other nodes.
def deploy_files(conn, root_dir, opts, master_nodes, slave_nodes, zoo_nodes,
        modules):
  active_master = master_nodes[0].public_dns_name

  num_disks = get_num_disks(opts.instance_type)
  hdfs_data_dirs = "/mnt/ephemeral-hdfs/data"
  mapred_local_dirs = "/mnt/hadoop/mrlocal"
  spark_local_dirs = "/mnt/spark"
  if num_disks > 1:
    for i in range(2, num_disks + 1):
      hdfs_data_dirs += ",/mnt%d/ephemeral-hdfs/data" % i
      mapred_local_dirs += ",/mnt%d/hadoop/mrlocal" % i
      spark_local_dirs += ",/mnt%d/spark" % i

  if zoo_nodes != []:
    zoo_list = '\n'.join([i.public_dns_name for i in zoo_nodes])
    cluster_url = "zoo://" + ",".join(
        ["%s:2181/mesos" % i.public_dns_name for i in zoo_nodes])
  else:
    zoo_list = "NONE"
    cluster_url = "%s:7077" % active_master

  template_vars = {
    "master_list": '\n'.join([i.public_dns_name for i in master_nodes]),
    "active_master": active_master,
    "slave_list": '\n'.join([i.public_dns_name for i in slave_nodes]),
    "zoo_list": zoo_list,
    "cluster_url": cluster_url,
    "hdfs_data_dirs": hdfs_data_dirs,
    "mapred_local_dirs": mapred_local_dirs,
    "spark_local_dirs": spark_local_dirs,
    "swap": str(opts.swap),
    "modules": '\n'.join(modules)
  }

  # Create a temp directory in which we will place all the files to be
  # deployed after we substitue template parameters in them
  tmp_dir = tempfile.mkdtemp()
  for path, dirs, files in os.walk(root_dir):
    if path.find(".svn") == -1:
      dest_dir = os.path.join('/', path[len(root_dir):])
      local_dir = tmp_dir + dest_dir
      if not os.path.exists(local_dir):
        os.makedirs(local_dir)
      for filename in files:
        if filename[0] not in '#.~' and filename[-1] != '~':
          dest_file = os.path.join(dest_dir, filename)
          local_file = tmp_dir + dest_file
          with open(os.path.join(path, filename)) as src:
            with open(local_file, "w") as dest:
              text = src.read()
              for key in template_vars:
                text = text.replace("{{" + key + "}}", template_vars[key])
              dest.write(text)
              dest.close()
  # rsync the whole directory over to the master machine
  command = (("rsync -rv -e 'ssh -o StrictHostKeyChecking=no -i %s' " +
      "'%s/' '%s@%s:/'") % (opts.identity_file, tmp_dir, opts.user, active_master))
  subprocess.check_call(command, shell=True)
  # Remove the temp directory we created above
  shutil.rmtree(tmp_dir)


# Copy a file to a given host through scp, throwing an exception if scp fails
def scp(host, opts, local_file, dest_file):
  subprocess.check_call(
      "scp -q -o StrictHostKeyChecking=no -i %s '%s' '%s@%s:%s'" %
      (opts.identity_file, local_file, opts.user, host, dest_file), shell=True)


# Run a command on a host through ssh, throwing an exception if ssh fails
def ssh(host, opts, command):
  tries = 0
  while True:
    try:
      return subprocess.check_call(
        "ssh -t -o StrictHostKeyChecking=no -i %s %s@%s '%s'" %
        (opts.identity_file, opts.user, host, command), shell=True)
    except subprocess.CalledProcessError as e:
      if (tries > 2):
        raise e
      print "Error connecting to host {0}, sleeping 30".format(e)
      time.sleep(30)
      tries = tries + 1

# Gets a list of zones to launch instances in
def get_zones(conn, opts):
  if opts.zone == 'all':
    zones = [z.name for z in conn.get_all_zones()]
  else:
    zones = [opts.zone]
  return zones


# Gets the number of items in a partition
def get_partition(total, num_partitions, current_partitions):
  num_slaves_this_zone = total / num_partitions
  if (total % num_partitions) - current_partitions > 0:
    num_slaves_this_zone += 1
  return num_slaves_this_zone

def wait_for_spark_cluster(master_nodes, opts):
  err = check_spark_cluster(master_nodes, opts)
  master = master_nodes[0].public_dns_name
  count = 0
  while err != 0 and count < 10:
    # try to restart spark
    ssh(master, opts, '/root/spark/bin/stop-all.sh')
    ssh(master, opts, '/root/spark/bin/start-all.sh')
    time.sleep(5)
    err = check_spark_cluster(master_nodes, opts)
    count = count + 1
  return err

def main():
  (opts, action, cluster_name) = parse_args()
  try:
    conn = ec2.connect_to_region(opts.region)
  except Exception as e:
    print >> stderr, (e)
    sys.exit(1)

  # Select an AZ at random if it was not specified.
  if opts.zone == "":
    opts.zone = random.choice(conn.get_all_zones()).name

  if action == "launch":
    if opts.resume:
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name)
    else:
      (master_nodes, slave_nodes, zoo_nodes) = launch_cluster(
          conn, opts, cluster_name)
      wait_for_cluster(conn, opts.wait, master_nodes, slave_nodes, zoo_nodes)
    setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, True)
    print "Waiting for cluster to start..."
    err = wait_for_spark_cluster(master_nodes, opts)
    if err != 0:
      print >> stderr, "ERROR: Cluster health check failed for spark_ec2"
      sys.exit(1)
    if opts.copy:
      copy_ampcamp_data_from_ebs(master_nodes, opts)
    print >>stderr, "SUCCESS: Cluster successfully launched! " + \
      "You can login to the master at " + master_nodes[0].public_dns_name

  elif action == "destroy":
    if not opts.destroy_noprompt:
      response = raw_input("Are you sure you want to destroy the cluster " +
          cluster_name + "?\nALL DATA ON ALL NODES WILL BE LOST!!\n" +
          "Destroy cluster " + cluster_name + " (y/N): ")
    else:
      response = "y"

    if response == "y":
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name, die_on_error=False)
      print "Terminating master..."
      for inst in master_nodes:
        inst.terminate()
      print "Terminating slaves..."
      for inst in slave_nodes:
        inst.terminate()
      if zoo_nodes != []:
        print "Terminating zoo..."
        for inst in zoo_nodes:
          inst.terminate()

      # Delete security groups as well
      if opts.delete_groups:
        print "Deleting security groups (this will take some time)..."
        group_names = [cluster_name + "-master", cluster_name + "-slaves", cluster_name + "-zoo"]

        attempt = 1;
        while attempt <= 3:
          print "Attempt %d" % attempt
          groups = [g for g in conn.get_all_security_groups() if g.name in group_names]
          success = True
          # Delete individual rules in all groups before deleting groups to
          # remove dependencies between them
          for group in groups:
            print "Deleting rules in security group " + group.name
            for rule in group.rules:
              for grant in rule.grants:
                  success &= group.revoke(ip_protocol=rule.ip_protocol,
                           from_port=rule.from_port,
                           to_port=rule.to_port,
                           src_group=grant)

          # Sleep for AWS eventual-consistency to catch up, and for instances
          # to terminate
          time.sleep(30)  # Yes, it does have to be this long :-(
          for group in groups:
            try:
              conn.delete_security_group(group.name)
              print "Deleted security group " + group.name
            except boto.exception.EC2ResponseError:
              success = False;
              print "Failed to delete security group " + group.name

          # Unfortunately, group.revoke() returns True even if a rule was not
          # deleted, so this needs to be rerun if something fails
          if success: break;

          attempt += 1

        if not success:
          print "Failed to delete all security groups after 3 tries."
          print "Try re-running in a few minutes."

  elif action == "login":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
        conn, opts, cluster_name)
    master = master_nodes[0].public_dns_name
    print "Logging into master " + master + "..."
    proxy_opt = ""
    if opts.proxy_port != None:
      proxy_opt = "-D " + opts.proxy_port
    subprocess.check_call("ssh -o StrictHostKeyChecking=no -i %s %s %s@%s" %
        (opts.identity_file, proxy_opt, opts.user, master), shell=True)

  elif action == "get-master":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(conn, opts, cluster_name)
    print master_nodes[0].public_dns_name

  elif action == "copy-data":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(conn, opts, cluster_name)
    print "Waiting for cluster to start..."
    err = wait_for_spark_cluster(master_nodes, opts)
    if err != 0:
      print >> stderr, "ERROR: Cluster health check failed for spark_ec2"
      sys.exit(1)
    copy_ampcamp_data_from_ebs(master_nodes, opts)
    print >>stderr, "SUCCESS: Data copied successfully! " + \
        "You can login to the master at " + master_nodes[0].public_dns_name

  elif action == "stop":
    response = raw_input("Are you sure you want to stop the cluster " +
        cluster_name + "?\nDATA ON EPHEMERAL DISKS WILL BE LOST, " +
        "BUT THE CLUSTER WILL KEEP USING SPACE ON\n" +
        "AMAZON EBS IF IT IS EBS-BACKED!!\n" +
        "Stop cluster " + cluster_name + " (y/N): ")
    if response == "y":
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name, die_on_error=False)
      print "Stopping master..."
      for inst in master_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.stop()
      print "Stopping slaves..."
      for inst in slave_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.stop()
      if zoo_nodes != []:
        print "Stopping zoo..."
        for inst in zoo_nodes:
          if inst.state not in ["shutting-down", "terminated"]:
            inst.stop()

  elif action == "start":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
        conn, opts, cluster_name)
    print "Starting slaves..."
    for inst in slave_nodes:
      if inst.state not in ["shutting-down", "terminated"]:
        inst.start()
    print "Starting master..."
    for inst in master_nodes:
      if inst.state not in ["shutting-down", "terminated"]:
        inst.start()
    if zoo_nodes != []:
      print "Starting zoo..."
      for inst in zoo_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.start()
    wait_for_cluster(conn, opts.wait, master_nodes, slave_nodes, zoo_nodes)
    setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, False)
    print "Waiting for cluster to start..."
    err = wait_for_spark_cluster(master_nodes, opts)
    if err != 0:
      print >> stderr, "ERROR: Cluster health check failed for spark_ec2"
      sys.exit(1)
    if opts.copy:
      copy_ampcamp_data_from_ebs(master_nodes, opts)
    print >>stderr, "SUCCESS: Cluster successfully launched! " + \
        "You can login to the master at " + master_nodes[0].public_dns_name

  else:
    print >> stderr, "Invalid action: %s" % action
    sys.exit(1)


if __name__ == "__main__":
  logging.basicConfig()
  main()
