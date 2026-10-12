from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3
from constructs import Construct


class S3Stack(Stack):
    """Media staging bucket for Instagram publishing (DESIGN.md section 9).

    Instagram's `image_url` and `video_url` must be publicly reachable; the
    container flow has Meta fetch the bytes before `media_publish`. We stage
    media in this bucket, hand Meta a short-lived presigned URL, and delete
    the object once the container reaches FINISHED. The 24-hour lifecycle
    rule backstops any publish that fails before the Publisher gets to
    `mediaStaging.drain`.

    SSE-S3 (AES-256) is sufficient here: the content lives for minutes and
    carries no credentials, so KMS adds cost without benefit. Versioning is
    off for the same reason; a mis-upload is replaced by the next
    `stage_media` call.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_project = f"{config['environment_name']}-{config['project_name']}"
        is_prod = config["environment_name"] == "prod"
        removal = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        self.media_bucket = s3.Bucket(
            self,
            "Media",
            bucket_name=f"{env_project}-media",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-staged-media",
                    enabled=True,
                    expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=removal,
            auto_delete_objects=not is_prod,
        )
