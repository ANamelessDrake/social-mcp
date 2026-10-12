import json

from aws_cdk import BundlingOptions, CfnOutput, Duration, Stack
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

# Lambda Web Adapter public layer (see https://github.com/awslabs/aws-lambda-web-adapter)
LWA_ACCOUNT = "753240598075"
LWA_LAYER_NAME = "LambdaAdapterLayerArm64"
LWA_LAYER_VERSION = "25"


class LambdaFunctionsStack(Stack):
    """The MCP server Lambda behind a streaming Function URL.

    Streamable HTTP MCP needs SSE passthrough; API Gateway and ALB buffer
    responses, so the front door is a Function URL in RESPONSE_STREAM mode
    with Lambda Web Adapter running the ASGI app (DESIGN.md section 2).

    The Publisher Lambda lands in M2 (publishing), along with its own
    construct for EventBridge Scheduler targets; M1 is read-only so this
    stack has one function.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: dict,
        secrets,
        database,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"

        lwa_layer = _lambda.LayerVersion.from_layer_version_arn(
            self,
            "LambdaWebAdapter",
            f"arn:aws:lambda:{self.region}:{LWA_ACCOUNT}:layer:{LWA_LAYER_NAME}:{LWA_LAYER_VERSION}",
        )

        # Asset root is lambdaFunctions/ so sharedModules/ can be bundled
        # alongside the function's own code.
        code = _lambda.Code.from_asset(
            "infrastructure/lambdaFunctions",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            # The function is arm64 but the bundling container
                            # matches the host, so pin pip to aarch64 wheels.
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

        # Static account roster (alias, provider, publish settings); dynamic
        # state (tokens, scopes, status) lives in the core table.
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

        self.mcp_function = _lambda.Function(
            self,
            "McpServer",
            function_name=f"{env_project}-mcp-server",
            description="Streamable HTTP MCP server for Facebook Pages and Instagram",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="run.sh",
            code=code,
            layers=[lwa_layer],
            memory_size=512,
            timeout=Duration.seconds(120),
            environment={
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_INVOKE_MODE": "response_stream",
                # Default is "/", which has no route and which the source-IP
                # allowlist refuses (the probe comes from inside the container).
                # /health is public by design and returns 200.
                "AWS_LWA_READINESS_CHECK_PATH": "/health",
                "PORT": "8000",
                "BEARER_TOKEN_SECRET": secrets.bearer_token.secret_name,
                "META_APP_SECRET": secrets.meta_app.secret_name,
                "CORE_TABLE": database.core_table.table_name,
                "TOKEN_KMS_KEY_ID": database.token_key.key_arn,
                "ACCOUNTS": accounts_env,
                "PUBLISHING_ENABLED": str(config.get("publishing_enabled", False)).lower(),
                # DNS names whose current IPs may call the server (empty = open).
                # A DDNS record here pins access to the operator's network.
                "ALLOWED_CLIENT_HOSTNAMES": ",".join(
                    config.get("allowed_client_hostnames", [])
                ),
            },
        )

        secrets.bearer_token.grant_read(self.mcp_function)
        secrets.meta_app.grant_read(self.mcp_function)
        database.core_table.grant_read_write_data(self.mcp_function)
        # Decrypt tokens at call time; encrypt rotated tokens for persistence
        # (DESIGN.md section 3.4).
        database.token_key.grant_encrypt_decrypt(self.mcp_function)

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
