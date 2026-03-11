###############################################################################
# DS5220 Data Project 1 – Terraform
# Equivalent to cloudformation-ds5220-dp1.yaml
#
# Usage:
#   terraform init
#   terraform apply
###############################################################################

terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}


# Variables


variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "your_ip_cidr" {
  description = "Your public IP in CIDR notation for SSH access, e.g. 199.111.219.79/32"
  type        = string
  default     = "199.111.219.79/32"
}

variable "s3_bucket_name" {
  description = "Globally unique S3 bucket name. Named with UVA computing ID: rna4ts"
  type        = string
  default     = "ds5220-dp1-bucket-rna4ts"
}

variable "key_pair_name" {
  description = "Name of an existing EC2 Key Pair for SSH access"
  type        = string
  default     = "DS5220"
}

variable "github_repo_url" {
  description = "HTTPS clone URL of your forked anomaly-detection repo"
  type        = string
  default     = "https://github.com/tiyeh21/DS-project-1-anomaly-detection-submission-repo.git"
}

variable "ami_id" {
  description = "Ubuntu 24.04 LTS AMI ID for us-east-1, verified 2025-12-12."
  type        = string
  default     = "ami-0b6c6ebed2801a5cb"
}


# SNS Topic
# Created before the bucket so the topic policy can be attached first,
# mirroring the DependsOn: SNSTopicPolicy in the CloudFormation template.


resource "aws_sns_topic" "dp1" {
  name = "ds5220-dp1"
}


# SNS Topic Policy – allows S3 to publish to the topic


resource "aws_sns_topic_policy" "dp1" {
  arn = aws_sns_topic.dp1.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3ToPublish"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.dp1.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:s3:::${var.s3_bucket_name}"
          }
        }
      }
    ]
  })
}


# S3 Bucket
# DependsOn equivalent: depends_on = [aws_sns_topic_policy.dp1]


resource "aws_s3_bucket" "anomaly" {
  bucket = var.s3_bucket_name

  tags = {
    Name = "ds5220-dp1-anomaly-detection"
  }

  depends_on = [aws_sns_topic_policy.dp1]
}


# S3 Event Notification → SNS
# Prefix: raw/   Suffix: .csv


resource "aws_s3_bucket_notification" "raw_csv" {
  bucket = aws_s3_bucket.anomaly.id

  topic {
    topic_arn     = aws_sns_topic.dp1.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "raw/"
    filter_suffix = ".csv"
  }

  depends_on = [aws_sns_topic_policy.dp1]
}


# IAM Role


resource "aws_iam_role" "ec2_role" {
  name = "ds5220-dp1-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}


# IAM Policy – full access to the single S3 bucket only


resource "aws_iam_role_policy" "s3_full_access" {
  name = "S3BucketFullAccess"
  role = aws_iam_role.ec2_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.anomaly.arn,
          "${aws_s3_bucket.anomaly.arn}/*"
        ]
      }
    ]
  })
}


# IAM Instance Profile


resource "aws_iam_instance_profile" "ec2_profile" {
  name = "ds5220-dp1-ec2-profile"
  role = aws_iam_role.ec2_role.name
}


# Security Group

resource "aws_security_group" "anomaly_sg" {
  name        = "ds5220-dp1-sg"
  description = "Allows SSH from your IP and FastAPI (port 8000) from anywhere"

  # SSH – restrict to YOUR IP
  ingress {
    description = "SSH from your IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.your_ip_cidr]
  }

  # FastAPI – open to the world so SNS can reach /notify
  ingress {
    description = "FastAPI / SNS notify"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "ds5220-dp1-sg"
  }
}


# EC2 Instance

resource "aws_instance" "anomaly" {
  ami                    = var.ami_id
  instance_type          = "t3.micro"
  key_name               = var.key_pair_name
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name
  vpc_security_group_ids = [aws_security_group.anomaly_sg.id]

  root_block_device {
    volume_size           = 16
    volume_type           = "gp3"
    delete_on_termination = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -e

    # 1. System packages
    apt-get update -y
    apt-get install -y git python3 python3-pip python3-venv

    # 2. Clone the forked application repository
    git clone ${var.github_repo_url} /opt/anomaly-detection

    # 3. Virtual environment + dependencies
    python3 -m venv /opt/anomaly-detection/venv
    /opt/anomaly-detection/venv/bin/pip install --upgrade pip
    /opt/anomaly-detection/venv/bin/pip install -r /opt/anomaly-detection/requirements.txt

    # 4. Environment variable – available immediately AND on reboot
    export BUCKET_NAME='${var.s3_bucket_name}'
    echo "BUCKET_NAME=${var.s3_bucket_name}" >> /etc/environment

    # 5. Systemd service so FastAPI starts on boot automatically
    cat > /etc/systemd/system/anomaly-detection.service << 'SYSTEMD_UNIT'
[Unit]
Description=Anomaly Detection FastAPI Service
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/anomaly-detection
EnvironmentFile=/etc/environment
ExecStart=/opt/anomaly-detection/venv/bin/fastapi run /opt/anomaly-detection/app.py --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SYSTEMD_UNIT

    # Make ubuntu the owner so the service can write logs etc.
    chown -R ubuntu:ubuntu /opt/anomaly-detection

    # Enable and start the service
    systemctl daemon-reload
    systemctl enable anomaly-detection
    systemctl start anomaly-detection
  EOF

  tags = {
    Name = "ds5220-dp1-anomaly-detection"
  }
}


# Elastic IP – attached to the instance


resource "aws_eip" "anomaly" {
  instance = aws_instance.anomaly.id
  domain   = "vpc"

  tags = {
    Name = "ds5220-dp1-eip"
  }
}


# SNS HTTP Subscription → http://<EIP>:8000/notify
# endpoint_auto_confirms = true so Terraform does not hang waiting for
# the SubscriptionConfirmation — the FastAPI /notify handler does it.


resource "aws_sns_topic_subscription" "http_notify" {
  topic_arn              = aws_sns_topic.dp1.arn
  protocol               = "http"
  endpoint               = "http://${aws_eip.anomaly.public_ip}:8000/notify"
  endpoint_auto_confirms = true
}


# Outputs


output "instance_elastic_ip" {
  description = "Elastic IP of the EC2 instance"
  value       = aws_eip.anomaly.public_ip
}

output "api_base_url" {
  description = "Base URL for the FastAPI service"
  value       = "http://${aws_eip.anomaly.public_ip}:8000"
}

output "notify_endpoint" {
  description = "SNS HTTP subscription endpoint"
  value       = "http://${aws_eip.anomaly.public_ip}:8000/notify"
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket"
  value       = aws_s3_bucket.anomaly.bucket
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic ds5220-dp1"
  value       = aws_sns_topic.dp1.arn
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = "ssh -i <your-key.pem> ubuntu@${aws_eip.anomaly.public_ip}"
}
