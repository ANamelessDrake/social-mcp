"""EventBridge Scheduler schedule group + IAM role for the Publisher.

Server-side scheduling (DESIGN.md section 7.3): the MCP server creates a
one-shot schedule in this group whose target is the Publisher Lambda,
with the draft id in the payload. EventBridge Scheduler fires at the
scheduled moment and the Publisher re-checks the approval hash + policy
before posting, so an edit between schedule and fire voids the approval.

The schedule group is a soft namespace per deployment (`{env}-socialmcp-
publish`); the role lives in this stack because its trust policy names
the Scheduler service and its permissions name the Publisher ARN, so
keeping it near the group avoids a cross-stack permissions tangle.
"""

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct


class SchedulerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: dict,
        publisher_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"

        self.schedule_group = scheduler.CfnScheduleGroup(
            self,
            "PublishGroup",
            name=f"{env_project}-publish",
        )

        self.schedule_group_name = self.schedule_group.name
        self.schedule_group_arn = (
            f"arn:aws:scheduler:{self.region}:{self.account}:schedule-group/"
            f"{self.schedule_group.name}"
        )

        self.scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{env_project}-scheduler",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="EventBridge Scheduler invokes the Publisher Lambda via this role",
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[publisher_arn, f"{publisher_arn}:*"],
            )
        )
