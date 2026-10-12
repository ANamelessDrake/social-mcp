from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from constructs import Construct


class DynamoDbStack(Stack):
    """The core table and token encryption key (DESIGN.md section 10).

    One table (`{env}-socialmcp-core`) holds every item family: account
    registry (PK=ACCOUNT#<alias>), drafts (PK=DRAFT#<id>, GSI1 for
    list_drafts), approvals (PK=APPROVAL#<draft_id>), and audit rows
    (PK=AUDIT#<account>, SK=TS#<iso>). Page and Instagram tokens live on the
    ACCOUNT items as ciphertext under the customer-managed key below.

    GSI1 is created in M1 so later milestones can land `list_drafts` without
    reshaping the table; the key is wide enough for both the drafts index
    (GSI1PK=DRAFTS#<account>) and future per-item indexes.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"
        is_prod = config["environment_name"] == "prod"
        removal = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        self.token_key = kms.Key(
            self,
            "TokenKey",
            alias=f"alias/{env_project}-tokens",
            description="Encrypts provider tokens stored in the core table",
            enable_key_rotation=True,
            removal_policy=removal,
        )

        self.core_table = dynamodb.Table(
            self,
            "Core",
            table_name=f"{env_project}-core",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=removal,
        )
        self.core_table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI1SK", type=dynamodb.AttributeType.STRING),
        )
