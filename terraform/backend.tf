terraform {
  backend "s3" {
    bucket = "<your-backend-s3-bucket>"
    key    = "<your-backend-s3-bucket-key-prefix"
    region = "<your-backend-s3-bucket-region>"

    encrypt      = true
    use_lockfile = true
  }
}
