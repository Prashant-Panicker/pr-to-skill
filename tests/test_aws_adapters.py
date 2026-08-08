import io
import unittest
from unittest.mock import Mock

from aws_adapters import DynamoDeliveryStore, S3ArtifactStore, SqsJobPublisher


class AwsAdapterTests(unittest.TestCase):
    def test_s3_artifact_store_reads_and_writes_under_prefix(self):
        client = Mock()
        client.get_object.return_value = {"Body": io.BytesIO(b'{"version": 1}')}
        store = S3ArtifactStore(client, "bucket", "demo")

        self.assertEqual(store.read_json("state.json"), {"version": 1})
        location = store.write_text("state.json", '{"version": 1}')

        self.assertEqual(location, "s3://bucket/demo/state.json")
        self.assertEqual(client.put_object.call_args.kwargs["Key"], "demo/state.json")
        self.assertEqual(
            client.put_object.call_args.kwargs["ServerSideEncryption"], "AES256"
        )

    def test_sqs_publisher_serializes_compact_job(self):
        client = Mock()
        publisher = SqsJobPublisher(client, "queue-url")

        publisher.publish({"version": 1, "work_type": "analysis"})

        self.assertEqual(client.send_message.call_args.kwargs, {
            "QueueUrl": "queue-url",
            "MessageBody": '{"version":1,"work_type":"analysis"}',
        })

    def test_dynamo_delivery_store_claims_and_releases_owned_lock(self):
        table = Mock()
        store = DynamoDeliveryStore(table, lease_seconds=60)

        with store.lock("job") as guard:
            guard.ensure_active()

        claim = table.put_item.call_args.kwargs
        self.assertEqual(claim["ConditionExpression"],
                         "attribute_not_exists(#key) OR expires_at < :now")
        release = table.delete_item.call_args.kwargs
        self.assertEqual(release["ConditionExpression"], "#owner = :owner")
        self.assertEqual(release["ExpressionAttributeNames"], {"#owner": "owner"})


if __name__ == "__main__":
    unittest.main()