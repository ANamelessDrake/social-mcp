#!/usr/bin/env python3
import json
import os
import sys

import aws_cdk as cdk
from aws_cdk import Environment

# Add the project root to the Python path so infrastructure/ imports resolve
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)

from infrastructure.stacks.dynamoDbStack import DynamoDbStack  # noqa: E402
from infrastructure.stacks.lambdaFunctionStack import LambdaFunctionsStack  # noqa: E402
from infrastructure.stacks.monitoringStack import MonitoringStack  # noqa: E402
from infrastructure.stacks.s3Stack import S3Stack  # noqa: E402
from infrastructure.stacks.schedulerStack import SchedulerStack  # noqa: E402
from infrastructure.stacks.secretsStack import SecretsStack  # noqa: E402

app = cdk.App()

# Load the environment from the context
environment = app.node.try_get_context("env")
if not environment:
    print("Error: No environment specified. Use --context env=<value>.")
    sys.exit(1)

# Load the matching config file
config_path = os.path.join(project_root, "config", f"{environment}.json")
try:
    with open(config_path) as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"Configuration file '{config_path}' not found. Copy config/example.json to start.")
    sys.exit(1)

env = Environment(account=config["account"], region=config.get("region", "us-east-1"))
env_project = f"{config['environment_name']}-{config['project_name']}"

secrets_stack = SecretsStack(
    app,
    f"{env_project}-secrets",
    config=config,
    env=env,
)

dynamodb_stack = DynamoDbStack(
    app,
    f"{env_project}-dynamodb",
    config=config,
    env=env,
)

s3_stack = S3Stack(
    app,
    f"{env_project}-s3",
    config=config,
    env=env,
)

lambda_stack = LambdaFunctionsStack(
    app,
    f"{env_project}-lambda",
    config=config,
    secrets=secrets_stack,
    database=dynamodb_stack,
    media=s3_stack,
    env=env,
)
lambda_stack.add_dependency(secrets_stack)
lambda_stack.add_dependency(dynamodb_stack)
lambda_stack.add_dependency(s3_stack)

# The Scheduler stack creates a role whose policy names the Publisher ARN,
# and the Lambda stack adds a policy to the MCP role naming the schedule
# group and scheduler role. Using CDK references in both directions would
# cycle the stacks. The function_name and schedule-group/scheduler-role
# names are deterministic from `{env}-{project}`, so we compose the ARNs
# as strings and break the cycle.
publisher_arn_str = (
    f"arn:aws:lambda:{env.region}:{env.account}:function:{env_project}-publisher"
)
schedule_group_name_str = f"{env_project}-publish"
schedule_group_arn_str = (
    f"arn:aws:scheduler:{env.region}:{env.account}:schedule-group/{schedule_group_name_str}"
)
scheduler_role_arn_str = f"arn:aws:iam::{env.account}:role/{env_project}-scheduler"

scheduler_stack = SchedulerStack(
    app,
    f"{env_project}-scheduler",
    config=config,
    publisher_arn=publisher_arn_str,
    env=env,
)
# Lambda stack only reads strings; no CDK dependency on the scheduler stack.
lambda_stack.wire_scheduler(
    schedule_group_name=schedule_group_name_str,
    schedule_group_arn=schedule_group_arn_str,
    scheduler_role_arn=scheduler_role_arn_str,
)

monitoring_stack = MonitoringStack(
    app,
    f"{env_project}-monitoring",
    config=config,
    mcp_function=lambda_stack.mcp_function,
    publisher_function=lambda_stack.publisher,
    env=env,
)
monitoring_stack.add_dependency(lambda_stack)

app.synth()
