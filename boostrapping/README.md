## Key Vault bootstrapping

#### Install az cli and run:

```
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
```

```
RESOURCE_GROUP=rg-homelab
TENANT_ID=$(az account show --query tenantId | tr -d \")
VAULT_NAME=kv-mkped-homelab
az keyvault create --name $VAULT_NAME --resource-group $RESOURCE_GROUP
``` 

#### Remember to do RBAC assignment before running the next:
```
az keyvault secret set --name secret-name --vault-name $VAULT_NAME --value "Azure Key Vault"
```

#### Create App Registration + Service Principle for authentication

```
APP_ID=$(az ad app create --display-name "external-secret-operator" --query appId | tr -d \")
SERVICE_PRINCIPAL=$(az ad sp create --id $APP_ID --query id | tr -d \")
```

Then assign RBAC to Service Principle for the Key Vault.

Then create two secrets for the clientid and client secret:

```
k create secret generic azure-secret-sp --from-literal=ClientID=$APP_ID --from-literal=ClientSecret=APP_PASSWORD
```