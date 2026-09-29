#to make it runable in 32bit os, please use python 3.5 32bit to make its exe.
import argparse,sys,os
import json

# from mrpa import Runner
parser=argparse.ArgumentParser(description="run a mrpa file with parameter")
parser.add_argument('--timeout',dest='timeout',type=int,default=60,help="set timeout to run the flow. Default value is 60s")
parser.add_argument('--host',dest='host',type=str,default="http://localhost:8700/",help='set the host to run the flow. Default value is http://localhost:8700/')
parser.add_argument('path', help="set the filepath of mrpa file to be run ")
parser.add_argument('--parameters', help="set the json file path for parameters to run the flow")

parameters={}
args=parser.parse_args()
if args.parameters:
    with open(args.parameters) as f:
        parameters=json.load(f)
#
# try:
#     runner=Runner(host=args.host,timeout=args.timeout,show_progress=True)
#     runner.upload_mrpa(args.path)
#     output,result=runner.run(params=parameters)
#     print("output: %s" % (output))
#     print("result: %s" % (result))
# except Exception as e:
#     print("Error: %s" % e)

