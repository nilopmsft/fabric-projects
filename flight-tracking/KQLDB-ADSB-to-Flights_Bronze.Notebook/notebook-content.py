# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "9e527ee1-1a9c-43fc-b636-ac2886a08762",
# META       "default_lakehouse_name": "ADSBLakehouse",
# META       "default_lakehouse_workspace_id": "dce12de4-879b-43f8-b593-179b45a4c990",
# META       "known_lakehouses": [
# META         {
# META           "id": "9e527ee1-1a9c-43fc-b636-ac2886a08762"
# META         }
# META       ]
# META     }
# META   }
# META }

# CELL ********************

# Sources
kustoUri = "https://trd-jkekdxkw9z64s153m9.z9.kusto.fabric.microsoft.com"
database="flight-metrics"

# Destinations
destination_table = "ADSBLakehouse.adsb_bronze" # Table where we are storing deltas

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

watermark="1970-01-01 00:00:00"
if spark.catalog.tableExists("ADSBLakehouse.adsb_bronze"):
    max_timestamp = spark.sql("SELECT MAX(timestamp) AS max_timestamp FROM %s"% (destination_table)).first()['max_timestamp']
    if max_timestamp:
       watermark = max_timestamp

print("Watermark being used: %s"%(watermark))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

#This is an example of Reading data from the KQL Database. Here the query retrieves the max,min fares and distances that the taxi recorded every month from the years 2014 to 2020

#from pyspark.sql.types import StringType, StructField, StructType, IntegerType

kustoQuery = "adsb | where timestamp > %s"% ( watermark )

# kustoQuery = "adsb \
# | extend epochTimestamp = datetime_diff('second', timestamp, datetime(1970-01-01 00:00:00)) \
# | where epochTimestamp > %s"% ( watermark )


#kustoSchema = StructType([
#    StructField("fields", StructType(), True),
#    StructField("name", StringType(), True),     
#    StructField("tags", StructType(), True),
#    StructField("timestamp", IntegerType(), True)
#])

kustoDf  = spark.read\
            .format("com.microsoft.kusto.spark.synapse.datasource")\
            .option("accessToken", mssparkutils.credentials.getToken(kustoUri))\
            .option("kustoCluster", kustoUri)\
            .option("kustoDatabase", database) \
            .option("kustoQuery", kustoQuery) \
            .load()

            #.schema(kustoSchema) \

print("Records to add: %s"%(kustoDf.count()))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

display(kustoDf.limit(10))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from pyspark.sql.functions import date_format, to_timestamp, col

kustoFormattedDf = kustoDf.withColumn(
          "year", date_format(to_timestamp(col('timestamp')), 'yyyy')) \
        .withColumn(
          "month", date_format(to_timestamp(col('timestamp')), 'MM')) \
        .withColumn(
          "day", date_format(to_timestamp(col('timestamp')), 'dd')) \
        .withColumn(
          "hour", date_format(to_timestamp(col('timestamp')), 'HH')) \
        .drop(
          "epochTimestamp"
        )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark",
# META   "frozen": false,
# META   "editable": true
# META }

# CELL ********************

display(kustoFormattedDf.limit(10))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

kustoFormattedDf.write.partitionBy("year","month","day","hour").mode("append").format("delta").saveAsTable(destination_table)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
