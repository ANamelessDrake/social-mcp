from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct


class MonitoringStack(Stack):
    """Operational alarms to the operator's email (DESIGN.md section 2).

    Personal-scale posture: a single topic and an error-rate alarm on the
    MCP function. Publish-volume alarms land with the Publisher Lambda in M2.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: dict,
        mcp_function,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"

        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{env_project}-alarms",
        )
        self.alarm_topic.add_subscription(
            subscriptions.EmailSubscription(config["alarm_email"])
        )

        errors_alarm = cloudwatch.Alarm(
            self,
            "McpServerErrors",
            alarm_name=f"{env_project}-mcp-server-errors",
            alarm_description="MCP server Lambda is erroring",
            metric=mcp_function.metric_errors(period=Duration.minutes(5)),
            threshold=5,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        errors_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))
