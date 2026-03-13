import eumdac
from config import EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET, EUM_COLLECTION

token = eumdac.AccessToken((EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET))
datastore = eumdac.DataStore(token)

c = datastore.get_collection(EUM_COLLECTION)
print("Collection object created:", c)

try:
    print("Search options:", c.search_options)
except Exception as e:
    print("SEARCH_OPTIONS_ERROR:", e)