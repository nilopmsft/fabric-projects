# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "4b44b336-f652-4368-a95e-12f9532b70e7",
# META       "default_lakehouse_name": "Mirroring_Monitoring_LH",
# META       "default_lakehouse_workspace_id": "f891c0cc-c9e2-420f-a211-965afa173f39",
# META       "known_lakehouses": [
# META         {
# META           "id": "4b44b336-f652-4368-a95e-12f9532b70e7"
# META         }
# META       ]
# META     }
# META   }
# META }

# MARKDOWN ********************

# ## Overview
# 
# This notebook is a simple iterative notebook that will make requests to all the various API endpoints for Fabric Mirrored database information. It performs the following retrieval steps:
# 
# - Secrets from a key vault for API access.
# - Capacities for the given tenant
# - Accessible* workspace information
# - Mirrored Databases in each workspace and their status
# - Table information belonging to the mirrored database(s)
# 
# This data is stored in panda dataframes and then written into a Lakehouse across multiple dim and fact tables.

# CELL ********************

import requests
import pandas as pd
from datetime import datetime

from pyspark.sql.functions import col, lit
from delta.tables import DeltaTable

# Time of notebook execution to use for status of all checks. 
# We do not care about granular accuracy as this check will be ran every few minutes at best so "aggregating" the facts in this runtime is acceptable.
notebook_run_time = datetime.utcnow().replace(microsecond=0)


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Authentication
# 
# We use an Azure Key Vault and Service Principal to call these API's. The requirements are
# 
# - Created Service principal that has API access at the Fabric Administrator level and minimum member access to mirroring workspaces. If the service principal does not have member or admin access to the workspace, it cannot see that workspace and could be missed for the inventory. Recommended to be part of a Entra Group for assignment
# - Accessible Keyvault that holds the service principal secrets. The secrets are
#     - tenantId
#     - clientId
#     - clientSecret

# CELL ********************

KEY_VAULT_NAME = "analytics-scus-nilop"

key_vault_uri = f"https://{KEY_VAULT_NAME}.vault.azure.net/"

try:

    tenant_id = notebookutils.credentials.getSecret(key_vault_uri, "tenantId")
    client_id = notebookutils.credentials.getSecret(key_vault_uri, "clientId")
    client_secret = notebookutils.credentials.getSecret(key_vault_uri, "clientSecret")
    print("Secrets retrieved successfully")
    
except Exception as e:
    print(f"Error retrieving secrets from Key Vault: {e}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

scope = "https://analysis.windows.net/powerbi/api/.default"

# Acquire an Azure AD access token for Microsoft Fabric APIs
token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
token_data = {
    "client_id": client_id,
    "client_secret": client_secret,
    "grant_type": "client_credentials",
    "scope": scope
}
token_response = requests.post(token_url, data=token_data)
token_response.raise_for_status()
access_token = token_response.json()["access_token"]

headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Get Capacities

# CELL ********************

# Get all distinct capacities and rename JSON fields to desired column names

capacities_url = "https://api.fabric.microsoft.com/v1/capacities"
capacities = []
while capacities_url:
    capacities_resp = requests.get(capacities_url, headers=headers)
    capacities_resp.raise_for_status()
    capacities_json = capacities_resp.json()
    capacities.extend(capacities_json.get("value", []))
    capacities_url = capacities_json.get("@odata.nextLink")  # handle paging if present

# Select/rename columns as needed for the DataFrame
renamed_capacities = []
for c in capacities:
    renamed_capacities.append({
        "capacity_id": c.get("id"),
        "capacity_name": c.get("displayName"),
        "capacity_state": c.get("state"),
        "capacity_sku": c.get("sku"),
        "capacity_region": c.get("region")
    })

capacities_status = []
for s in capacities:
    capacities_status.append({
        "capacity_id": s.get("id"),
        "capacity_sku": s.get("sku"),
        "capacity_state": s.get("state"),
        "status_date": notebook_run_time
    })

capacities_df = pd.DataFrame(renamed_capacities)
capacities_status_df= pd.DataFrame(capacities_status)

display(capacities_df)
display(capacities_status_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Get Workspaces

# CELL ********************

#List all workspaces in the tenant
workspaces_url = "https://api.fabric.microsoft.com/v1/workspaces"
workspaces = []
while workspaces_url:
    ws_resp = requests.get(workspaces_url, headers=headers)
    ws_resp.raise_for_status()
    ws_json = ws_resp.json()
    workspaces.extend(ws_json.get("value", []))
    workspaces_url = ws_json.get("@odata.nextLink")  # handle paging if present

renamed_workspaces = []
for w in workspaces:
    renamed_workspaces.append({
        "workspace_id": w.get("id"),
        "workspace_name": w.get("displayName"),
        "workspace_type": w.get("type"),
        "capacity_id": w.get("capacityId"),
    })

workspaces_df = pd.DataFrame(renamed_workspaces)

display(workspaces_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Get Databases

# CELL ********************

# Each workspace, get mirrored databases (with deduplication)
mirrored_db_rows = []
databases_mirroring_status_rows = []

seen_db_ids = set()
for ws in workspaces:
    workspace_id = ws.get("id")
    api_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases"
    r = requests.get(api_url, headers=headers)
    if r.status_code == 200:
        dbs = r.json().get("value", []) if isinstance(r.json(), dict) else r.json()
        for db in dbs:
            db_id = db.get("id")
            if db_id not in seen_db_ids:
                seen_db_ids.add(db_id)
                mirrored_db_rows.append({
                    "db_id": db.get("id"),
                    "db_name": db.get("displayName"),
                    "workspace_id": workspace_id
                })
    else:
        print(f"Failed to get mirrored databases for workspace {workspace_id} ({workspace_name}): {r.status_code}: {r.text}")

# If we found mirrored databases, get the status of them to add to our fact table
if mirrored_db_rows:
    databases_df = pd.DataFrame(mirrored_db_rows)
    for idx, row in databases_df.iterrows():
        workspace_id = row["workspace_id"]
        db_id = row["db_id"]

        # Fetch mirroring status for each mirrored database
        mirroring_status_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases/{db_id}/getMirroringStatus"
        resp = requests.post(mirroring_status_url, headers=headers)
        if resp.status_code == 200:
            status_json = resp.json()
            databases_mirroring_status_rows.append({
                "db_id": db_id,
                "mirroring_status": status_json.get("status"),
                "status_date": notebook_run_time
            })

    databases_mirroring_status_df = pd.DataFrame(databases_mirroring_status_rows)
    display(databases_df)
    display(databases_mirroring_status_df)
else:
    print("No mirrored databases found.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Get Tables

# CELL ********************

tables_rows = []
tables_mirroring_status_rows = []

# Cleanup timestamp to second granularity, e.g. 1970-01-01 00:00:00
def format_api_timestamp(timestamp: str) -> str:
    if not timestamp:
        return None
        
    try:
        dot_index = timestamp.find('.')
        if dot_index != -1:
            string_to_parse = timestamp[:dot_index]
        else:
            string_to_parse = timestamp.rstrip('Z')

        dt_object = datetime.strptime(string_to_parse, "%Y-%m-%dT%H:%M:%S")

        return dt_object.strftime("%Y-%m-%d %H:%M:%S")
        
    except ValueError as e:
        print(f"Error parsing timestamp '{timestamp}': {e}")
        return None

for idx, row in databases_df.iterrows():
    workspace_id = row["workspace_id"]
    mirrored_db_id = row["db_id"]

    # Get tables mirroring status/metrics
    tables_status_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases/{mirrored_db_id}/getTablesMirroringStatus"
    ts_resp = requests.post(tables_status_url, headers=headers)
    if ts_resp.status_code == 200:
        tables = ts_resp.json()
        # We expect 'value' to be a list with results per table; add workspace and database IDs to each record
        for table in tables.get("data", []) if isinstance(tables, dict) else tables:
            table_id = table.get("sourceTableName")+ "_" + mirrored_db_id
            raw_sync_datetime = table.get("metrics", {}).get("lastSyncDateTime")
            formatted_sync_datetime = format_api_timestamp(raw_sync_datetime)
            tables_rows.append({
                "table_id": table_id,
                "source_table_name": table.get("sourceTableName"),                
                "source_schema_name": table.get("sourceSchemaName"),
                "source_object_type": table.get("sourceObjectType"),
                "db_id": mirrored_db_id

            })
            tables_mirroring_status_rows.append({
                "table_id": table_id,
                "table_mirror_status": table.get("status"),
                "table_processed_rows": table.get("metrics", {}).get("processedRows"),
                "table_processed_bytes": table.get("metrics", {}).get("processedBytes"),
                #"table_last_sync_date_time": table.get("metrics", {}).get("lastSyncDateTime"),
                "table_last_sync_date_time": formatted_sync_datetime,
                "table_last_sync_latency_in_seconds": table.get("metrics", {}).get("lastSyncLatencyInSeconds"),
                "status_date": notebook_run_time
            })

# Convert results to DataFrames
tables_df = pd.DataFrame(tables_rows)
tables_mirroring_status_df = pd.DataFrame(tables_mirroring_status_rows)

# Show results
display(tables_df)
display(tables_mirroring_status_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Write Capacity data to Lakehouse

# CELL ********************

# Write Capacity information to Lakehouse in SCD2 for dimensions as well as capacity fact table

# Prepare timestamp values
now = datetime.utcnow()
future = datetime(9999, 12, 31, 0, 0, 0)

# Prepare the new dataframe for incoming batch
if isinstance(capacities_df, pd.DataFrame):
    new_cap_df = spark.createDataFrame(capacities_df)
else:
    new_cap_df = capacities_df

new_cap_df = new_cap_df.withColumn("row_start", lit(now)).withColumn("row_end", lit(future)).withColumn("is_current", lit(True))

# Check if "dim_capacities" table exists
if "dim_capacities" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it, all rows are set as current/historic-begin
    new_cap_df.write.format("delta").mode("overwrite").saveAsTable("dim_capacities")
    print("dim_capacities table created")
else:
    # Table exists—carry out SCD2 merge
    delta_table = DeltaTable.forName(spark, "dim_capacities")
    delta_table.alias("lakehouse").merge(
        new_cap_df.alias("incoming"),
        "lakehouse.capacity_id = incoming.capacity_id AND lakehouse.is_current = true"
    ).whenMatchedUpdate(
        condition="""
            lakehouse.capacity_sku != incoming.capacity_sku OR
            lakehouse.capacity_name != incoming.capacity_name OR
            lakehouse.capacity_region != incoming.capacity_region
        """,
        set={
            "row_end": lit(now),
            "is_current": lit(False)
        }
    ).whenNotMatchedInsert(
        values={
            "capacity_id": col("incoming.capacity_id"),
            "capacity_name": col("incoming.capacity_name"),
            "capacity_sku": col("incoming.capacity_sku"),
            "capacity_state": col("incoming.capacity_state"),
            "capacity_region": col("incoming.capacity_region"),
            "row_start": lit(now),
            "row_end": lit(future),
            "is_current": lit(True)
        }
    ).execute()
    print("dim_capacities updated latest capacity information, using SCD Type 2 logic.")


# Write out capacity status to lakehouse
if isinstance(capacities_status_df, pd.DataFrame):
    capacities_status_df = spark.createDataFrame(capacities_status_df)
else:
    capacities_status_df = capacities_status_df

if "fact_capacity_status" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it
    capacities_status_df.write.format("delta").mode("overwrite").saveAsTable("fact_capacity_status")
    print("fact_capacity_status table created")
else:
    capacities_status_df.write.format("delta").mode("append").saveAsTable("fact_capacity_status")
    print("capacities_status_df appended to existing fact_capacity_status table")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Write Workspace data to Lakehouse

# CELL ********************

# Write Workspace information to Lakehouse in SCD2 for dimensions

# Prepare timestamp values
now = datetime.utcnow()
future = datetime(9999, 12, 31, 0, 0, 0)

# Prepare the new dataframe for incoming batch
if isinstance(workspaces_df, pd.DataFrame):
    new_workspace_df = spark.createDataFrame(workspaces_df)
else:
    new_workspace_df = capacities_df

new_workspace_df = new_workspace_df.withColumn("row_start", lit(now)).withColumn("row_end", lit(future)).withColumn("is_current", lit(True))

# Check if "dim_workspaces" table exists
if "dim_workspaces" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it, all rows are set as current/historic-begin
    new_workspace_df.write.format("delta").mode("overwrite").saveAsTable("dim_workspaces")
    print("dim_workspaces table created")
else:
    # Table exists—carry out SCD2 merge
    delta_table = DeltaTable.forName(spark, "dim_workspaces")
    delta_table.alias("lakehouse").merge(
        new_workspace_df.alias("incoming"),
        "lakehouse.workspace_id = incoming.workspace_id AND lakehouse.is_current = true"
    ).whenMatchedUpdate(
        condition="""
            lakehouse.workspace_name != incoming.workspace_name OR
            lakehouse.workspace_type != incoming.workspace_type OR
            lakehouse.capacity_id != incoming.capacity_id
        """,
        set={
            "row_end": lit(now),
            "is_current": lit(False)
        }
    ).whenNotMatchedInsert(
        values={
            "workspace_id": col("incoming.workspace_id"),
            "workspace_name": col("incoming.workspace_name"),
            "workspace_type": col("incoming.workspace_type"),
            "capacity_id": col("incoming.capacity_id"),
            "row_start": lit(now),
            "row_end": lit(future),
            "is_current": lit(True)
        }
    ).execute()
    print("dim_workspaces updated latest workspace information, using SCD Type 2 logic.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Write Database data to Lakehouse

# CELL ********************

# Write Database information to Lakehouse in SCD2 for dimensions and mirroring status fact table

from delta.tables import DeltaTable

# Convert pandas to Spark DataFrame if necessary
if isinstance(databases_df, pd.DataFrame):
    databases_df = spark.createDataFrame(databases_df)
else:
    databases_df = databases_df

table_name = "dim_databases"
db_id_col = "db_id"

if table_name not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it
    databases_df.write.format("delta").mode("overwrite").saveAsTable(table_name)
    print(f"{table_name} table created")
else:
    # Do upsert: only insert if db_id does not exist
    delta_table = DeltaTable.forName(spark, table_name)
    # All columns to be inserted
    cols = databases_df.columns
    insert_dict = {col: f"source.{col}" for col in cols}
    delta_table.alias("target").merge(
        databases_df.alias("source"),
        f"target.{db_id_col} = source.{db_id_col}"
    ).whenNotMatchedInsert(values=insert_dict).execute()
    print(f"Only new db_id values inserted to {table_name} (existing db_ids not duplicated)")

# The logic for databases mirror status table remains as before
if isinstance(databases_mirroring_status_df, pd.DataFrame):
    databases_mirroring_status_df = spark.createDataFrame(databases_mirroring_status_df)
else:
    databases_mirroring_status_df = databases_mirroring_status_df

if "fact_database_mirroring_status" not in [t.name for t in spark.catalog.listTables()]:
    databases_mirroring_status_df.write.format("delta").mode("overwrite").saveAsTable("fact_database_mirroring_status")
    print("fact_database_mirroring_status table created")
else:
    databases_mirroring_status_df.write.format("delta").mode("append").saveAsTable("fact_database_mirroring_status")
    print("databases_mirroring_status_df appended to existing fact_database_mirroring_status table")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Write Table data to Lakehouse

# CELL ********************

# Write out tables to lakehouse via dimension and fact table

if isinstance(tables_df, pd.DataFrame):
    tables_df = spark.createDataFrame(tables_df)
else:
    tables_df = tables_df

table_name = "dim_tables"
table_id_col = "table_id"

if "dim_tables" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it
    tables_df.write.format("delta").mode("overwrite").saveAsTable("dim_tables")
    print("dim_databases table created")
else:
    # Do upsert: only insert if table_id does not exist
    delta_table = DeltaTable.forName(spark, table_name)
    # All columns to be inserted
    cols = tables_df.columns
    insert_dict = {col: f"source.{col}" for col in cols}
    delta_table.alias("target").merge(
        tables_df.alias("source"),
        f"target.{table_id_col} = source.{table_id_col}"
    ).whenNotMatchedInsert(values=insert_dict).execute()
    print(f"Only new table_id values inserted to {table_name} (existing table_id not duplicated)")

# Write out tables mirroring status to Lakehouse
if isinstance(tables_mirroring_status_df, pd.DataFrame):
    tables_mirroring_status_df = spark.createDataFrame(tables_mirroring_status_df)
else:
    tables_mirroring_status_df = tables_mirroring_status_df

if "fact_table_mirroring_status" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it
    tables_mirroring_status_df.write.format("delta").mode("overwrite").saveAsTable("fact_table_mirroring_status")
    print("fact_table_mirroring_status table created")
else:
    tables_mirroring_status_df.write.format("delta").mode("append").saveAsTable("fact_table_mirroring_status")
    print("tables_mirroring_status_df appended to existing fact_table_mirroring_status table")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Show All Results

# CELL ********************

display(capacities_df)
display(workspaces_df)
display(databases_df)
display(tables_df)
display(capacities_status_df)
display(databases_mirroring_status_df)
display(tables_mirroring_status_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark",
# META   "frozen": false,
# META   "editable": true
# META }

# CELL ********************


# Option 1: Pandas approach

import pandas as pd

# 1) Load the Spark table into a pandas DataFrame
pdf = spark.table("fact_table_mirroring_status").toPandas()

# 2) Ensure status_date is datetime, then drop microseconds
#    - dt.floor('S') rounds down to the nearest second
pdf["status_date"] = pd.to_datetime(pdf["status_date"], errors="coerce").dt.floor("S")

# 4) (Optional) Push back to Spark as a new table
spark_df = spark.createDataFrame(pdf)
spark_df.write.mode("overwrite").saveAsTable("fact_table_mirroring_status")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
