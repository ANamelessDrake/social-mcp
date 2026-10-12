from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class SecretsStack(Stack):
    """Deployment-level credentials (DESIGN.md section 4).

    Per-account Meta tokens do NOT live here; they are KMS-encrypted items in
    DynamoDB (DESIGN.md section 3.4). Secrets Manager holds only what belongs
    to the deployment itself:

    - BearerToken keeps its generated value; that is the token MCP clients present.
    - MetaApp is created with a placeholder. After registering the Meta app
      (docs/meta-setup.md), store the real credentials as JSON:

          aws secretsmanager put-secret-value --secret-id <name> \\
              --secret-string '{"app_id": "...", "app_secret": "..."}'
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = f"{config['environment_name_upper']}-{config['project_name_upper']}"
        # Real Meta app credentials should survive accidental stack deletion in prod
        self._removal_policy = (
            RemovalPolicy.RETAIN
            if config["environment_name"] == "prod"
            else RemovalPolicy.DESTROY
        )

        self.bearer_token = self._secret(
            f"{prefix}-BearerToken",
            "Static bearer token for MCP clients (deployment-local auth)",
            # This value gets pasted into shell env files, .mcp.json, and
            # systemd units. Secrets Manager's default charset includes `, $, |
            # and friends, which break a sourced env file in confusing ways;
            # 32 alphanumerics still carry ~190 bits of entropy.
            #
            # WARNING: editing this block on an existing deployment REGENERATES
            # the token (CloudFormation replaces the secret value), silently
            # breaking every client holding the old one. Re-read the secret and
            # re-key clients after any change here.
            generate=secretsmanager.SecretStringGenerator(
                password_length=32,
                exclude_punctuation=True,
            ),
        )
        self.meta_app = self._secret(
            f"{prefix}-MetaApp",
            "Meta Business app credentials JSON (app_id, app_secret)",
        )

    def _secret(
        self,
        name: str,
        description: str,
        generate: secretsmanager.SecretStringGenerator | None = None,
    ) -> secretsmanager.Secret:
        secret = secretsmanager.Secret(
            self,
            name,
            secret_name=name,
            description=description,
            generate_secret_string=generate,
        )
        secret.apply_removal_policy(self._removal_policy)
        return secret
