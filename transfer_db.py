from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from urllib.parse import quote_plus

# Old MongoDB Atlas URI
OLD_URI = "mongodb+srv://nagonu:0500868021Yaw@cluster0.yp3zg2d.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# New MongoDB Atlas URI (URL-encode credentials to handle special chars)
NEW_USERNAME = "zico_cybok"
NEW_PASSWORD = "T7uF10RDgC5Im7Wp"
NEW_URI = (
    f"mongodb+srv://{quote_plus(NEW_USERNAME)}:{quote_plus(NEW_PASSWORD)}"
    "@cluster0.a77dwo1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)

DB_NAME = "zico"
BATCH_SIZE = 1000
DROP_TARGET_COLLECTIONS = False


def copy_indexes(old_coll, new_coll):
    for idx in old_coll.list_indexes():
        if idx.get("name") == "_id_":
            continue
        keys = list(idx["key"].items())
        options = {k: v for k, v in idx.items() if k not in ("key", "ns", "v")}
        name = options.pop("name", None)
        new_coll.create_index(keys, name=name, **options)


def iter_batches(old_coll):
    last_id = None
    while True:
        query = {"_id": {"$gt": last_id}} if last_id is not None else {}
        batch_docs = list(
            old_coll.find(query).sort("_id", 1).limit(BATCH_SIZE)
        )
        if not batch_docs:
            break
        last_id = batch_docs[-1]["_id"]
        yield batch_docs


def copy_collection(old_coll, new_coll):
    if DROP_TARGET_COLLECTIONS:
        new_coll.drop()

    copied = 0
    for batch in iter_batches(old_coll):
        new_coll.insert_many(batch, ordered=False)
        copied += len(batch)
        print(f"  Inserted {copied} docs into {new_coll.name}")

    copy_indexes(old_coll, new_coll)


def main():
    old_client = MongoClient(OLD_URI, server_api=ServerApi("1"))
    new_client = MongoClient(NEW_URI, server_api=ServerApi("1"))

    old_db = old_client[DB_NAME]
    new_db = new_client[DB_NAME]

    collections = old_db.list_collection_names()
    if not collections:
        print(f"No collections found in old database '{DB_NAME}'.")
        return

    print(f"Copying database '{DB_NAME}' with {len(collections)} collections...")
    for name in collections:
        print(f"Copying collection: {name}")
        copy_collection(old_db[name], new_db[name])

    print("Done.")


if __name__ == "__main__":
    main()
