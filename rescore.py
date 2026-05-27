"""One-shot script: reset SCORE_FAILED items to PENDING and re-trigger scoring."""
import boto3

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
lambda_client = boto3.client('lambda', region_name='us-east-1')
table = dynamodb.Table('job-agent-prod-jobs')

# Scan for SCORE_FAILED items
failed = []
last_key = None
while True:
    kwargs = {'FilterExpression': boto3.dynamodb.conditions.Attr('status').eq('SCORE_FAILED')}
    if last_key:
        kwargs['ExclusiveStartKey'] = last_key
    response = table.scan(**kwargs)
    failed.extend(response['Items'])
    last_key = response.get('LastEvaluatedKey')
    if not last_key:
        break

print(f"Found {len(failed)} SCORE_FAILED items")

# Process in batches: re-invoke scorer directly with each as a fake stream event
import json
for i, item in enumerate(failed):
    payload = {
        "Records": [{
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "pk": {"S": item["pk"]},
                    "company": {"S": item.get("company", "Unknown")},
                    "title": {"S": item.get("title", "Unknown")},
                    "location": {"S": item.get("location", "Unknown")},
                    "description": {"S": item.get("description", "")},
                    "source": {"S": item.get("source", "unknown")},
                    "remote": {"BOOL": bool(item.get("remote", False))},
                    "apply_url": {"S": item.get("apply_url", "")},
                }
            }
        }]
    }
    try:
        lambda_client.invoke(
            FunctionName='job-agent-prod-scorer',
            InvocationType='Event',  # async fire-and-forget
            Payload=json.dumps(payload),
        )
        if (i + 1) % 50 == 0:
            print(f"Triggered {i+1}/{len(failed)}")
    except Exception as e:
        print(f"Failed to trigger for {item['pk']}: {e}")

print(f"Done. Triggered scoring for {len(failed)} items.")
print("Wait ~3-5 minutes for scoring to complete, then run the notifier.")