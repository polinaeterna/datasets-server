## AWS CLI profile

To work on the `datasets-server` infrastructure, you have to configure AWS to use the SSO account `hub` (see https://huggingface.awsapps.com/start#/) with the role `EKS-HUB-Hub` (see also the [doc in Notion about AWS SSO](https://www.notion.so/huggingface2/Conventions-645d29ce0a01496bb07c67a06612aa98#ff642cd8e28a4107ae26cc6183ccdd01)):

```shell
$ aws configure sso
SSO start URL [None]: https://huggingface.awsapps.com/start#/
SSO Region [None]: us-east-1
There are 3 AWS accounts available to you. # <-- select "hub"
Using the account ID 707930574880
There are 3 roles available to you. # <-- select "EKS-HUB-Hub"
Using the role name "EKS-HUB-Hub"
CLI default client Region [None]: us-east-1
CLI default output format [None]:
CLI profile name [EKS-HUB-Hub-707930574880]: hub-prod

To use this profile, specify the profile name using --profile, as shown:

aws s3 ls --profile hub-prod
```

In the docs, we assume the AWS CLI profile is called `hub-prod`.

The profile `hub-prod` is meant to:

- operate inside the two EKS clusters (`hub-prod` and `hub-ephemeral`):

  ```shell
  $ aws eks describe-cluster --profile=hub-prod --name=hub-prod
  $ aws eks update-kubeconfig --profile=hub-prod --name=hub-prod
  ```

- list, pull, push docker images from repositories of the ECR registry (`707930574880.dkr.ecr.us-east-1.amazonaws.com`):

  ```shell
  $ aws ecr get-login-password --region us-east-1 --profile=hub-prod \
    | docker login --username AWS --password-stdin 707930574880.dkr.ecr.us-east-1.amazonaws.com
  ```

  **Note**: the `EKS-HUB-Hub` profile still misses this right. Until the infra team adds it, you can use the `hub-pu` profile.

It is not meant to operate on AWS resources directly. The following command gives authentication error for example:

```shell
$ aws eks list-clusters --profile=hub-prod
```