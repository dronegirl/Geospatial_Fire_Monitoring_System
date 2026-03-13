import eumdac
from config import EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET, EUM_COLLECTION

if not EUMETSAT_CONSUMER_KEY or not EUMETSAT_CONSUMER_SECRET:
    raise ValueError("Set EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET first.")

token = eumdac.AccessToken((EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET))
datastore = eumdac.DataStore(token)

collection = datastore.get_collection(EUM_COLLECTION)
print("Connected successfully.")
print("Collection:", collection)