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
import time
import random

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, lit
from delta.tables import DeltaTable

# Time of notebook execution to use for status of all checks. 
# We do not care about granular accuracy as this check will be ran every few minutes at best so "aggregating" the facts in this runtime is acceptable.
notebook_run_time = datetime.utcnow().replace(microsecond=0)

parallelism = 5

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

# CELL ********************

def random_plus_minus_percentage(value, percentage=10):
    """
    Returns the value randomly adjusted by ±percentage%.
    
    Args:
        value (float or int): The original number.
        percentage (float): The percentage range for adjustment (default 10).
    
    Returns:
        float: Adjusted value.
    """
    # Validate inputs
    if not isinstance(value, (int, float)):
        raise TypeError("Value must be an integer or float.")
    if not (0 <= percentage <= 100):
        raise ValueError("Percentage must be between 0 and 100.")
    
    # Calculate the adjustment factor
    factor = 1 + random.uniform(-percentage / 100, percentage / 100)
    return value * factor

def get_api_with_throttle(url, headers, method="get"):
    while True:
        response = None
        if method == "get":
            response = requests.get(url, headers=headers)
        elif method == "post":
            response = requests.post(url, headers=headers)
        else:
            raise Exception(f"Method {method} invalid.")
        if response.status_code == 429:
            retry_after = random_plus_minus_percentage(int(response.headers.get("Retry-After", 1)), 10)
            time.sleep(retry_after)
        elif response.status_code == 200:
            return response.json()
        else: 
            raise Exception(f"{response.status_code} not valid for url {url}. Please Try Again")

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
    capacities_json = get_api_with_throttle(capacities_url, headers=headers)
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

capacities_df = spark.sparkContext.parallelize(renamed_capacities).toDF()
capacities_status_df = spark.sparkContext.parallelize(capacities_status).toDF()

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
    ws_json = get_api_with_throttle(workspaces_url, headers=headers)
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

workspaces_df = spark.sparkContext.parallelize(renamed_workspaces, parallelism).toDF()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ### Get Databases

# CELL ********************

# Each workspace, get mirrored databases (with deduplication)

def get_mirrored_databases(workspace, headers):
    mirrored_db_rows = []
    workspace_id = workspace["workspace_id"]
    api_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases"
    r = get_api_with_throttle(api_url, headers=headers)
    dbs = r.get("value", []) if isinstance(r, dict) else r
    for db in dbs:
        mirrored_db_rows.append({
            "db_id": db.get("id"),
            "db_name": db.get("displayName"),
            "workspace_id": workspace_id
        })
    if len(mirrored_db_rows) > 0:
        return mirrored_db_rows

def get_mirrored_db_status(mirrored_db, headers):
    workspace_id = mirrored_db["workspace_id"]
    db_id = mirrored_db["db_id"]
    databases_mirroring_status_rows = []
    # Fetch mirroring status for each mirrored database
    mirroring_status_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases/{db_id}/getMirroringStatus"

    status_json = get_api_with_throttle(mirroring_status_url, headers=headers, method="post")
    databases_mirroring_status_rows.append({
        "db_id": db_id,
        "mirroring_status": status_json.get("status"),
        "status_date": notebook_run_time
    })
    return databases_mirroring_status_rows

mirrored_db_rows = workspaces_df.rdd.map(lambda ws: get_mirrored_databases(ws, headers))
# Remove results from workspaces without mirroring
mirrored_db_rows = mirrored_db_rows.filter(lambda x: x != None).flatMap(lambda x: x)

databases_df = mirrored_db_rows.toDF()
# If we found mirrored databases, get the status of them to add to our fact table
databases_mirroring_status_df = None
if databases_df.count() > 0:
    
    databases_mirroring_status = databases_df.rdd.map(lambda db: get_mirrored_db_status(db, headers))
    # Remove blank results, map properties to top level, and covert to spark df
    databases_mirroring_status_df = databases_mirroring_status.filter(lambda x: x != None).flatMap(lambda x: x).toDF()

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

def get_table_status(mirrored_db, headers):
    tables_rows = []
    workspace_id = mirrored_db["workspace_id"]
    mirrored_db_id = mirrored_db["db_id"]

    # Get tables mirroring status/metrics
    tables_status_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/mirroredDatabases/{mirrored_db_id}/getTablesMirroringStatus"

    tables = get_api_with_throttle(tables_status_url, headers=headers, method="post")
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
            "db_id": mirrored_db_id,
            "table_mirror_status": table.get("status"),
            "table_processed_rows": table.get("metrics", {}).get("processedRows"),
            "table_processed_bytes": table.get("metrics", {}).get("processedBytes"),
            "table_last_sync_date_time": formatted_sync_datetime,
            "table_last_sync_latency_in_seconds": table.get("metrics", {}).get("lastSyncLatencyInSeconds"),
            "status_date": notebook_run_time
        })
    return tables_rows

tables_all_status = databases_df.rdd.map(lambda db: get_table_status(db, headers))

tables_all_status_df = tables_all_status.filter(lambda x: x != None).flatMap(lambda x: x).toDF()
tables_df = tables_all_status_df
tables_mirroring_status_df = tables_all_status_df
# Convert results to DataFrames
tables_df = tables_df.select("table_id", "source_table_name", "source_schema_name", "source_object_type", "db_id")
tables_mirroring_status_df = tables_mirroring_status_df.select(   "table_id", 
                                                            "table_mirror_status",
                                                            "table_processed_rows",
                                                            "table_processed_bytes",
                                                            "table_last_sync_date_time",
                                                            "table_last_sync_latency_in_seconds",
                                                            "status_date")

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

capacities_df = capacities_df.withColumn("row_start", lit(now)).withColumn("row_end", lit(future)).withColumn("is_current", lit(True))

# Check if "dim_capacities" table exists
if "dim_capacities" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it, all rows are set as current/historic-begin
    capacities_df.write.format("delta").mode("overwrite").saveAsTable("dim_capacities")
    print("dim_capacities table created")
else:
    # Table exists—carry out SCD2 merge
    delta_table = DeltaTable.forName(spark, "dim_capacities")
    delta_table.alias("lakehouse").merge(
        capacities_df.alias("incoming"),
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

workspaces_df = workspaces_df.withColumn("row_start", lit(now)).withColumn("row_end", lit(future)).withColumn("is_current", lit(True))

# Check if "dim_workspaces" table exists
if "dim_workspaces" not in [t.name for t in spark.catalog.listTables()]:
    # Table does not exist—create it, all rows are set as current/historic-begin
    workspaces_df.write.format("delta").mode("overwrite").saveAsTable("dim_workspaces")
    print("dim_workspaces table created")
else:
    # Table exists—carry out SCD2 merge
    delta_table = DeltaTable.forName(spark, "dim_workspaces")
    delta_table.alias("lakehouse").merge(
        workspaces_df.alias("incoming"),
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
