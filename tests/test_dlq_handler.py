import json
from unittest.mock import MagicMock

from src.modules.transcription.queue.dlq_handler import handle_failure
from src.shared.error_context import build_error_context
from src.shared.errors import PermanentPipelineError, TransientPipelineError

QUEUE_URL = "https://sqs.example/main"
DLQ_URL = "https://sqs.example/dlq"
MESSAGE = {"ReceiptHandle": "rh-123", "MessageId": "msg-abc"}
ORIGINAL_BODY = {
    "station": "radio_test",
    "s3_input": "s3://bucket/audio.mp3",
    "s3_output_prefix": "s3://bucket/out/radio_test",
}


def test_permanent_error_forwards_to_dlq_and_deletes_from_main():
    sqs = MagicMock()
    error = PermanentPipelineError("audio corrupto")
    ctx = build_error_context(error, module="transcribe", job_id="msg-abc", audio_ref=ORIGINAL_BODY["s3_input"], attempt=1)

    action = handle_failure(sqs, QUEUE_URL, DLQ_URL, MESSAGE, ORIGINAL_BODY, error, ctx)

    assert action == "dlq_immediate"
    sqs.send_message.assert_called_once()
    sent_kwargs = sqs.send_message.call_args.kwargs
    assert sent_kwargs["QueueUrl"] == DLQ_URL
    envelope = json.loads(sent_kwargs["MessageBody"])
    assert envelope["original_job"] == ORIGINAL_BODY
    assert envelope["error"]["job_id"] == "msg-abc"
    assert envelope["error"]["error_type"] == "PermanentPipelineError"

    sqs.delete_message.assert_called_once_with(QueueUrl=QUEUE_URL, ReceiptHandle="rh-123")
    sqs.change_message_visibility.assert_not_called()


def test_transient_error_extends_visibility_and_does_not_delete():
    sqs = MagicMock()
    error = TransientPipelineError("rate limit")
    ctx = build_error_context(error, module="segmentation", job_id="msg-abc", audio_ref=None, attempt=2)

    action = handle_failure(sqs, QUEUE_URL, DLQ_URL, MESSAGE, ORIGINAL_BODY, error, ctx)

    assert action == "retry_backoff"
    sqs.change_message_visibility.assert_called_once_with(
        QueueUrl=QUEUE_URL, ReceiptHandle="rh-123", VisibilityTimeout=90,
    )
    sqs.send_message.assert_not_called()
    sqs.delete_message.assert_not_called()
