# AWS login refresh needs a session region

Found during the real S3 CLI smoke test on 2026-09-29.

With Boto3 1.43.104 and CRT installed, cached `aws login` credentials worked until
refresh was needed. The default AWS profile contained `login_session` but no
region. Supplying `region_name` only to `session.client("s3", ...)` did not provide
a region for the credential provider's internally created Sign-In client. Refresh
failed with `botocore.exceptions.NoRegionError`.

The S3 adapter now sets the configured region on `boto3.Session` itself, as well
as on the S3 client. The standalone live test does the same. Rerunning the test
successfully refreshed the login session and passed all 13 live checks.

This needs no new authentication flow. It uses the existing AWS login session.
