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
        publisher_function=None,
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

        if publisher_function is not None:
            # Any Publisher error reaches here; a bad publish is loud (DESIGN.md 7.6).
            publisher_errors = cloudwatch.Alarm(
                self,
                "PublisherErrors",
                alarm_name=f"{env_project}-publisher-errors",
                alarm_description="Publisher Lambda is erroring",
                metric=publisher_function.metric_errors(period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            publisher_errors.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

            # Publish volume anomaly: more than N invocations in 5 minutes is a
            # cue to look (one bad agent batch-publishing, for example).
            publisher_volume = cloudwatch.Alarm(
                self,
                "PublisherVolume",
                alarm_name=f"{env_project}-publisher-volume",
                alarm_description="Publisher Lambda invocation volume spike",
                metric=publisher_function.metric_invocations(period=Duration.minutes(5)),
                threshold=20,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            publisher_volume.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))
