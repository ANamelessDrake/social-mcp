import json

from aws_cdk import BundlingOptions, CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

# Lambda Web Adapter public layer (see https://github.com/awslabs/aws-lambda-web-adapter)
LWA_ACCOUNT = "753240598075"
LWA_LAYER_NAME = "LambdaAdapterLayerArm64"
LWA_LAYER_VERSION = "25"


class LambdaFunctionsStack(Stack):
    """MCP server + Publisher (DESIGN.md section 2).

    The MCP server lives behind a streaming Function URL (SSE passthrough
    requires RESPONSE_STREAM, which API Gateway and ALB break). The
    Publisher is a plain function invoked directly, by the MCP server
    (synchronous RequestResponse for `publish_draft`) or by EventBridge
    Scheduler (for `schedule_draft`); it is the only path that calls
    Meta's publish endpoints.

    `schedule_group_arn` is passed in via `wire_scheduler` once
    SchedulerStack has created the group, so the MCP server's role can
    be narrowed to CreateSchedule / DeleteSchedule on just that group.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: dict,
        secrets,
        database,
        media=None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"

        lwa_layer = _lambda.LayerVersion.from_layer_version_arn(
            self,
            "LambdaWebAdapter",
            f"arn:aws:lambda:{self.region}:{LWA_ACCOUNT}:layer:{LWA_LAYER_NAME}:{LWA_LAYER_VERSION}",
        )

        mcp_code = _lambda.Code.from_asset(
            "infrastructure/lambdaFunctions",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "pip install -r mcpServer/requirements.txt -t /asset-output"
                            " --platform manylinux2014_aarch64 --implementation cp"
                            " --python-version 3.12 --only-binary=:all:",
                            "cp -r mcpServer/. /asset-output/",
                            "cp -r sharedModules /asset-output/sharedModules",
                            "chmod +x /asset-output/run.sh",
                        ]
                    ),
                ],
            ),
        )

        publisher_code = _lambda.Code.from_asset(
            "infrastructure/lambdaFunctions",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "pip install -r publisher/requirements.txt -t /asset-output"
                            " --platform manylinux2014_aarch64 --implementation cp"
                            " --python-version 3.12 --only-binary=:all:",
                            "cp -r publisher/. /asset-output/",
                            "cp -r sharedModules /asset-output/sharedModules",
                        ]
                    ),
                ],
            ),
        )

        accounts_env = json.dumps(
            [
                {
                    "alias": a["alias"],
                    "provider": a["provider"],
                    "publish_enabled": bool(a.get("publish_enabled", False)),
                    "publish_mode": a.get("publish_mode", "manual"),
                    "auto_for_kinds": list(a.get("auto_for_kinds", [])),
                    "policy": a.get("policy", {}),
                }
                for a in config.get("accounts", [])
            ]
        )

        shared_env = {
            "CORE_TABLE": database.core_table.table_name,
            "TOKEN_KMS_KEY_ID": database.token_key.key_arn,
            "META_APP_SECRET": secrets.meta_app.secret_name,
            "ACCOUNTS": accounts_env,
            "PUBLISHING_ENABLED": str(config.get("publishing_enabled", False)).lower(),
        }
        if media is not None:
            # Instagram publishing needs a public URL for every media container;
            # the Publisher presigns an object from this bucket and tears it
            # down after FINISHED (DESIGN.md section 9).
            shared_env["MEDIA_BUCKET"] = media.media_bucket.bucket_name

        self.publisher = _lambda.Function(
            self,
            "Publisher",
            function_name=f"{env_project}-publisher",
            description="Posts approved drafts to Meta (DESIGN.md section 2)",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="app.handler",
            code=publisher_code,
            memory_size=512,
            timeout=Duration.seconds(300),
            environment=shared_env,
        )
        secrets.meta_app.grant_read(self.publisher)
        database.core_table.grant_read_write_data(self.publisher)
        database.token_key.grant_encrypt_decrypt(self.publisher)
        if media is not None:
            # Publisher reads staged bytes to presign and deletes them after
            # the container reaches FINISHED (DESIGN.md section 9).
            media.media_bucket.grant_read(self.publisher)
            media.media_bucket.grant_delete(self.publisher)

        self.mcp_function = _lambda.Function(
            self,
            "McpServer",
            function_name=f"{env_project}-mcp-server",
            description="Streamable HTTP MCP server for Facebook Pages and Instagram",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="run.sh",
            code=mcp_code,
            layers=[lwa_layer],
            memory_size=512,
            timeout=Duration.seconds(120),
            environment={
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_INVOKE_MODE": "response_stream",
                "AWS_LWA_READINESS_CHECK_PATH": "/health",
                "PORT": "8000",
                "BEARER_TOKEN_SECRET": secrets.bearer_token.secret_name,
                "PUBLISHER_FUNCTION_ARN": self.publisher.function_arn,
                "ALLOWED_CLIENT_HOSTNAMES": ",".join(
                    config.get("allowed_client_hostnames", [])
                ),
                **shared_env,
            },
        )
        secrets.bearer_token.grant_read(self.mcp_function)
        secrets.meta_app.grant_read(self.mcp_function)
        database.core_table.grant_read_write_data(self.mcp_function)
        database.token_key.grant_encrypt_decrypt(self.mcp_function)
        if media is not None:
            # stage_media writes staged bytes under staging/<account>/<ulid>/;
            # delete rights let a cross-account rejection in draft_post clean
            # up its own object rather than leaving it for the lifecycle rule.
            media.media_bucket.grant_write(self.mcp_function)
            media.media_bucket.grant_delete(self.mcp_function)

        # MCP server invokes the Publisher synchronously for publish_draft
        self.publisher.grant_invoke(self.mcp_function)

        self.function_url = self.mcp_function.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.NONE,
            invoke_mode=_lambda.InvokeMode.RESPONSE_STREAM,
        )

        CfnOutput(
            self,
            "McpEndpoint",
            value=f"{self.function_url.url}mcp",
            description="MCP endpoint URL for client configuration",
        )

    def wire_scheduler(
        self,
        *,
        schedule_group_name: str,
        schedule_group_arn: str,
        scheduler_role_arn: str,
    ) -> None:
        """Grant the MCP server rights to create and delete schedules in the group.

        Called from `bin/app.py` after `SchedulerStack` creates the group,
        so the schedule group ARN is resolvable. The permission is scoped
        to the single group this deployment uses.
        """
        self.mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:DeleteSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:UpdateSchedule",
                ],
                # Schedule ARNs are schedule-group/{group}/{schedule_name}
                resources=[f"{schedule_group_arn.replace('schedule-group/', 'schedule/')}/*"],
            )
        )
        self.mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[scheduler_role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        self.mcp_function.add_environment("SCHEDULE_GROUP_NAME", schedule_group_name)
        self.mcp_function.add_environment("SCHEDULER_ROLE_ARN", scheduler_role_arn)
