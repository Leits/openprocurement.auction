import logging
import copy
import sys
from ..templates import prepare_service_stage
from ..utils import calculate_hash
from ..utils import (
    get_tender_data,
    get_latest_bid_for_bidder,
    patch_tender_data
)
from ..systemd_msgs_ids import(
    AUCTION_WORKER_API_AUCTION_CANCEL,
    AUCTION_WORKER_API_AUCTION_NOT_EXIST,
    AUCTION_WORKER_SERVICE_NUMBER_OF_BIDS,
    AUCTION_WORKER_API_APPROVED_DATA,
    AUCTION_WORKER_SET_AUCTION_URLS
)
from barbecue import calculate_coeficient

MULTILINGUAL_FIELDS = ['title', 'description']
ADDITIONAL_LANGUAGES = ['ru', 'en']
ROUNDS = 3
logger = logging.getLogger('Auction Worker')


def get_auction_info(self, prepare=False):
    if not self.debug:
        if prepare:
            self._auction_data = get_tender_data(
                self.tender_url,
                request_id=self.request_id,
                session=self.session
            )
        else:
            self._auction_data = {'data': {}}
        auction_data = get_tender_data(
            self.tender_url + '/auction',
            user=self.worker_defaults['TENDERS_API_TOKEN'],
            request_id=self.request_id,
            session=self.session
        )
        if auction_data:
            self._auction_data['data'].update(auction_data['data'])
            del auction_data
        else:
            self.get_auction_document()
            if self.auction_document:
                self.auction_document['current_stage'] = -100
                self.save_auction_document()
                logger.warning('Cancel auction: {}'.format(
                    self.auction_doc_id
                ), extra={'JOURNAL_REQUEST_ID': self.request_id,
                          'MESSAGE_ID': AUCTION_WORKER_API_AUCTION_CANCEL})
            else:
                logger.error('Auction {} not exists'.format(
                    self.auction_doc_id
                ), extra={'JOURNAL_REQUEST_ID': self.request_id,
                          'MESSAGE_ID': AUCTION_WORKER_API_AUCTION_NOT_EXIST})
            sys.exit(1)
    self._lot_data = dict({item['id']: item for item in self._auction_data['data']['lots']}[self.lot_id])
    self._lot_data['items'] = [item for item in self._auction_data['data'].get('items', [])
                               if item['relatedLot'] == self.lot_id]
    self._lot_data['features'] = [item for item in self._auction_data['data'].get('features', [])
                                  if item['relatedLot'] == self.lot_id]
    self.startDate = self.convert_datetime(
        self._lot_data['auctionPeriod']['startDate']
    )
    self.bidders_features = None
    self.features = None
    if not prepare:
        self.bidders_data = []
        for bid_index, bid in enumerate(self._auction_data['data']['bids']):
            for lot_index, lot_bid in enumerate(bid['lotValues']):
                if lot_bid['relatedLot'] == self.lot_id:
                    bid_data = {
                        'id': bid['id'],
                        'date': lot_bid['date'],
                        'value': lot_bid['value']
                    }
                    if 'parameters' in lot_bid:
                        bid_data['parameters'] = copy.copy(lot_bid['parameters'])
                    self.bidders_data.append(bid_data)
        self.bidders_count = len(self.bidders_data)
        logger.info('Bidders count: {}'.format(self.bidders_count),
                    extra={'JOURNAL_REQUEST_ID': self.request_id,
                           'MESSAGE_ID': AUCTION_WORKER_SERVICE_NUMBER_OF_BIDS})
        self.rounds_stages = []
        for stage in range((self.bidders_count + 1) * ROUNDS + 1):
            if (stage + self.bidders_count) % (self.bidders_count + 1) == 0:
                self.rounds_stages.append(stage)
        self.mapping = {}
        if self._lot_data.get('features', None):
            self.bidders_features = {}
            self.bidders_coeficient = {}
            self.features = self._lot_data['features']
            for bid in self.bidders_data:
                self.bidders_features[bid['id']] = bid['parameters']
                self.bidders_coeficient[bid['id']] = calculate_coeficient(self.features, bid['parameters'])
        else:
            self.bidders_features = None
            self.features = None

        for index, uid in enumerate(self.bidders_data):
            self.mapping[self.bidders_data[index]['id']] = str(index + 1)


def prepare_auction_document(self):
    self.auction_document.update(
        {'_id': self.auction_doc_id,
         'stages': [],
         'tenderID': self._auction_data['data'].get('tenderID', ''),
         'TENDERS_API_VERSION': self.worker_defaults['TENDERS_API_VERSION'],
         'initial_bids': [],
         'current_stage': -1,
         'results': [],
         'minimalStep': self._lot_data.get('minimalStep', {}),
         'procuringEntity': self._auction_data['data'].get('procuringEntity', {}),
         'items': self._lot_data.get('items', []),
         'value': self._lot_data.get('value', {}),
         'lot': {}}
    )
    if self.features:
        self.auction_document['auction_type'] = 'meat'
    else:
        self.auction_document['auction_type'] = 'default'

    for key in MULTILINGUAL_FIELDS:
        for lang in ADDITIONAL_LANGUAGES:
            lang_key = '{}_{}'.format(key, lang)
            if lang_key in self._auction_data['data']:
                self.auction_document[lang_key] = self._auction_data['data'][lang_key]
            if lang_key in self._lot_data:
                self.auction_document.lot[lang_key] = self._lot_data[lang_key]
        self.auction_document[key] = self._auction_data['data'].get(key, '')
        self.auction_document['lot'][key] = self._lot_data.get(key, '')

    self.auction_document['stages'].append(
        prepare_service_stage(
            start=self.startDate.isoformat(),
            type="pause"
        )
    )
    return self.auction_document


def prepare_auction_and_participation_urls(self):
    auction_url = self.worker_defaults['AUCTIONS_URL'].format(
        auction_id=self.auction_doc_id
    )
    patch_data = {'data': {'lots': list(self._auction_data['data']['lots']),
                           'bids': list(self._auction_data['data']['bids'])}}
    for index, lot in enumerate(self._auction_data['data']['lots']):
        if lot['id'] == self.lot_id:
            patch_data['data']['lots'][index]['auctionUrl'] = auction_url
            break

    for bid_index, bid in enumerate(self._auction_data['data']['bids']):
        for lot_index, lot_bid in enumerate(bid['lotValues']):
            if lot_bid['relatedLot'] == self.lot_id:

                participation_url = self.worker_defaults['AUCTIONS_URL'].format(
                    auction_id=self.auction_doc_id
                )
                participation_url += '/login?bidder_id={}&hash={}'.format(
                    bid['id'],
                    calculate_hash(bid['id'], self.worker_defaults['HASH_SECRET'])
                )
                patch_data['data']['bids'][bid_index]['lotValues'][lot_index]['participationUrl'] = participation_url
                break
    logger.info("Set auction and participation urls for tender {}".format(self.tender_id),
                extra={"JOURNAL_REQUEST_ID": self.request_id,
                       "MESSAGE_ID": AUCTION_WORKER_SET_AUCTION_URLS})
    logger.info(repr(patch_data))
    patch_tender_data(self.tender_url + '/auction/{}'.format(self.lot_id), patch_data,
                      user=self.worker_defaults["TENDERS_API_TOKEN"],
                      request_id=self.request_id, session=self.session)
    return patch_data


def post_results_data(self):
    all_bids = self.auction_document["results"]
    logger.info(
        "Approved data: {}".format(all_bids),
        extra={"JOURNAL_REQUEST_ID": self.request_id,
               "MESSAGE_ID": AUCTION_WORKER_API_APPROVED_DATA}
    )

    patch_data = {'data': {'bids': list(self._auction_data['data']['bids'])}}
    for bid_index, bid in enumerate(self._auction_data['data']['bids']):
        for lot_index, lot_bid in enumerate(bid['lotValues']):
            if lot_bid['relatedLot'] == self.lot_id:
                auction_bid_info = get_latest_bid_for_bidder(all_bids, bid["id"])
                patch_data['data']['bids'][bid_index]['lotValues'][lot_index]["value"]["amount"] = auction_bid_info["amount"]
                patch_data['data']['bids'][bid_index]['lotValues'][lot_index]["date"] = auction_bid_info["time"]
                break
    results = patch_tender_data(
        self.tender_url + '/auction/{}'.format(self.lot_id), data=patch_data,
        user=self.worker_defaults["TENDERS_API_TOKEN"],
        method='post',
        request_id=self.request_id, session=self.session
    )
    return results


def announce_results_data(self, results=None):
    if not results:
        results = get_tender_data(
            self.tender_url,
            user=self.worker_defaults["TENDERS_API_TOKEN"],
            request_id=self.request_id,
            session=self.session
        )
    bids_information = {}
    for bid in self._auction_data['data']['bids']:
        for lot_bid in bid['lotValues']:
            if lot_bid['relatedLot'] == self.lot_id:
                bids_information[bid['id']] = bid["tenderers"]
                break

    for section in ['initial_bids', 'stages', 'results']:
        for index, stage in enumerate(self.auction_document[section]):
            if 'bidder_id' in stage and stage['bidder_id'] in bids_information:
                self.auction_document[section][index]["label"]["uk"] = bids_information[stage['bidder_id']][0]["name"]
                self.auction_document[section][index]["label"]["ru"] = bids_information[stage['bidder_id']][0]["name"]
                self.auction_document[section][index]["label"]["en"] = bids_information[stage['bidder_id']][0]["name"]
    self.auction_document["current_stage"] = (len(self.auction_document["stages"]) - 1)

    return None
