import logging
import struct
import pdb
import bencode
import constants
import torrent
from tornado.options import options
from connection import Connection
import math
from util import parse_bitmask

class BTMessageHandler(object):
    def __init__(self, application, request):
        self.application = application
        self.request = request
        self._finished = False

    def write(self, data, callback=None):
        self.request.connection.write(data, callback)

    def finish(self):
        self._finished = True
        self.request.finish()
        self._log()

    def _log(self):
        self.application.log_request(self)

    def _request_summary(self):
        if self.request.connection.torrent and self.request.connection.torrent.hash:
            hashstr = '(' + self.request.connection.torrent.hash[:6] + '..)'
        else:
            hashstr = '--'

        if self.request.type == 'PIECE':
            otherinfo = str(len(self.request.payload))
        else:
            otherinfo = str(self.request.args)

        return hashstr + ' ' + self.request.type + " " + otherinfo + ' ' +\
            " (" + self.request.remote_ip + ")"

    def send_message(self, type, payload=None, log=True):
        self.request.connection.send_message(type, payload, log)
                
    def enqueue_message(self, type, payload):
        self.request.connection.enqueue_message( (type, payload) )

    def send_some_shit(self):
        if self.request.connection._remote_bitmask:
            logging.info('remote is interested and we have their bitfield... %s' % self.request.connection._remote_bitmask)
            count = 0

            for index,have in enumerate(self.request.connection._remote_bitmask):
                if not have:
                    count += 1
                    t = torrent.Torrent.instantiate( self.request.connection.handshake['infohash'] )

                    offset = 0
                    logging.info('selected piece %s and offset %s' % (index, offset))

                    sz = 2**14

                    offset = 0

                    if self.request.connection._remote_choked:
                        return

                    while offset < t.get_piece_len():
                        data = t.get_piece(index).get_data(offset, sz)
                        payload = ''.join((
                            struct.pack('>I',index),
                            struct.pack('>I',offset),
                            data))
                        self.send_message('PIECE', payload, log=False)
                        offset += sz
                    

                    if count > 10:
                        break
            return True
        else:
            logging.error('dont have remote bitfield')
            self.finish()


class NullHandler(BTMessageHandler):
    def handle(self):
        logging.info('nullhandler handle message %s' % [self.request, self.request.type, self.request.payload])
        self.finish()

class BitmaskHandler(BTMessageHandler):

    def handle(self):
        if not self.request.connection.torrent.meta:
            logging.warn('bitfield not useful unless we know number of pieces')
            self.request.connection._stored_bitmask = self.request.payload
            self.finish()
            return
        pieces_len = self.request.len
        self.request.connection._remote_bitmask = parse_bitmask(self.request.connection.torrent, self.request.payload)
        self.request.connection._remote_bitmask_incomplete = self.request.connection._remote_bitmask.count(0)

        logging.info('remo.bitmask is %s' % ''.join(map(str,self.request.connection._remote_bitmask)))

        self.request.connection.send_bitmask()
        self.finish()

from constants import tor_meta_codes, tor_meta_codes_r, HANDSHAKE_CODE

class UTHandler(BTMessageHandler):
    def handle(self):
        ext_msg_type = ord(self.request.payload[0])
        

        if ext_msg_type == HANDSHAKE_CODE:
            info = bencode.bdecode(self.request.payload[1:])
            logging.info('got extension data %s' % info)
            # handshake

            self.request.connection._remote_extension_handshake = info
            self.request.connection._remote_extension_handshake_r = dict( (v,k) for k,v in info['m'].items() )

            if not self.request.connection._sent_extension_handshake:

                resp = {'v': 'ktorrent 0.01',
                        'm': {},
                        'p': options.port}
                if self.request.connection.torrent and self.request.connection.torrent.meta:
                    resp['metadata_size'] = len(self.request.connection.torrent.meta_info)

                if 'ut_metadata' in self.request.connection._remote_extension_handshake['m']:
                    # this is not necessary to match the remote's codes
                    code = self.request.connection._remote_extension_handshake['m']['ut_metadata']
                    resp['m']['ut_metadata'] = code

                self.request.connection._my_extension_handshake = resp
                self.request.connection._my_extension_handshake_codes = dict( (v,k) for k,v in resp['m'].items() )
                logging.info('sending ext msg %s' % resp)
                # send handshake message

                self.send_message('UTORRENT_MSG', chr(HANDSHAKE_CODE) + bencode.bencode(resp), log=False)

        elif self.request.connection._my_extension_handshake_codes and ext_msg_type in self.request.connection._my_extension_handshake_codes:

            ext_msg_str = self.request.connection._my_extension_handshake_codes[ext_msg_type]

            logging.info('handling %s message' % ext_msg_str)
            if ext_msg_str == 'ut_metadata':

                i = self.request.payload.find('total_size') # this payload is not bencoded (the payload includes the piece outside of the bencoded dictionary, so we have to search (unfortunate) ...
                if i != -1:
                    # this is a piece request response (if an exception occurs here, then perhaps it is another type of message. should verify the msg_type
                    j = self.request.payload.find('ee',i)
                    if j != -1:
                        data = self.request.payload[1:j+2]
                        meta = bencode.bdecode(data)
                        rest = self.request.payload[j+2:]
                        self.request.connection.insert_meta_piece(meta['piece'],rest)
                else:
                    info = bencode.bdecode( self.request.payload[1:] )
                    tor_meta_type = tor_meta_codes[ info['msg_type'] ]

                    if tor_meta_type == 'request':

                        if self.request.connection.torrent and self.request.connection.torrent.meta:
                            logging.info('have torrent file... will service the metadata chunk request!')
                            payload = self.request.connection.torrent.get_metadata_piece_payload(info['piece'])
                            self.send_message('UTORRENT_MSG', chr(ext_msg_type) + payload)
                        else:
                            logging.error('dont have torrent matadata cant serve it!')
                            # todo: send deny message
                            deny_payload = bencode.bencode( { 'msg_type': tor_meta_codes_r['reject'] } )


                            self.send_message('UTORRENT_MSG', chr(ext_msg_type) + deny_payload)
            else:
                logging.error('unhandled metadata extension %s' % ext_msg_str)
                if options.asserts:
                    pdb.set_trace()

        else:
            logging.error('do not recognize extension message type %s' % ext_msg_type)
            if options.asserts:
                pdb.set_trace()

        self.finish()

class ChokeHandler(BTMessageHandler):
    def handle(self):
        logging.info('got choke message :-(')
        self.request.connection._am_choked = True
        self.finish()

class UnChokeHandler(BTMessageHandler):
    def handle(self):
        logging.info('got unchoke message')
        self.request.connection._am_choked = False
        self.finish()

class HaveAllHandler(BTMessageHandler):
    def handle(self):
        if self.request.connection.torrent.meta:
            self.request.connection._remote_bitmask[index] = [1] * len(self.request.connection.torrent.bitmask)
        else:
            logging.warn('dont know how to handle have all because we dont have torrent meta')
        self.finish()

class HaveHandler(BTMessageHandler):
    def handle(self):

        if not self.request.connection._remote_bitmask:
            if self.request.connection.torrent and self.request.connection.torrent.bitmask:
                self.request.connection._remote_bitmask = [0] * len(self.request.connection.torrent.bitmask)
            else:
                logging.error('they sent us a have but we dont have torrent meta !!')
            # initialize an empty bitmask

        index = struct.unpack('>I', self.request.payload)[0]
        self.request.args = [index]

        if not self.request.connection._remote_bitmask:
            self.request.connection._stored_haves.append(index)
            logging.info('storing have message for later when we get torrent meta')
            self.finish()
            return

        #logging.info('have index: %s' % index)
        slot = self.request.connection._remote_bitmask[index]
        self.request.connection._remote_bitmask[index] = 1
        self.request.connection._remote_bitmask_incomplete = self.request.connection._remote_bitmask.count(0)
        logging.info('remote now has %s incomplete pieces' % self.request.connection._remote_bitmask_incomplete)
        #logging.info('changing slot from %s to %s' % (slot, 1))
        self.finish()

class PortHandler(BTMessageHandler):
    def handle(self):
        port = struct.unpack('>H', self.request.payload)[0]
        logging.info('extension message got port %s' % port)
        self.args = [port]
        self.finish()

class NotInterestedHandler(BTMessageHandler):
    def handle(self):
        self.request.connection._remote_interested = False
        tosuggest = self.request.connection._remote_bitmask.index(0)
        self.send_message('SUGGEST_PIECE', struct.pack('>I', tosuggest))
        self.finish()

class RequestHandler(BTMessageHandler):
    def handle(self):
        index = struct.unpack('>I', self.request.payload[0:4])[0]
        offset = struct.unpack('>I', self.request.payload[4:8])[0]
        sz = struct.unpack('>I', self.request.payload[8:12])[0]
        self.request.args = [index, offset, sz]
        #logging.info('request %s %s %s' % (index, offset, sz))

        if self.request.connection.torrent.bitmask[index] == 1:
            data = self.request.connection.torrent.get_piece(index).get_data(offset, sz)
            payload = ''.join((
                    struct.pack('>I',index),
                    struct.pack('>I',offset),
                    data))
            self.send_message('PIECE', payload, log=True)
            self.finish()
        else:
            self.send_message('REJECT_REQUEST', self.request.payload)
            self.finish()

class InterestedHandler(BTMessageHandler):
    def handle(self):
        self.request.connection._remote_interested = True
        self.send_message('UNCHOKE') # :-)
        self.finish()
        
            
class CancelHandler(BTMessageHandler):
    def handle(self):
        self.finish()

class PieceHandler(BTMessageHandler):
    def handle(self):
        self.request.connection._piece_request_outbound -= 1

        index = struct.unpack('>I', self.request.payload[0:4])[0]
        offset = struct.unpack('>I', self.request.payload[4:8])[0]
        self.request.args = [index, offset, '...']
        data = self.request.payload[8:]
        tor_finished, piece_finished = self.request.connection.torrent.get_piece(index).handle_peer_response(self, offset, data)
        conn = self.request.connection

        #if conn._piece_request_outbound == 0:
        #    Connection.make_piece_request(conn)

        if tor_finished:
            logging.info('torrent finished! :-)')
            # don't send the last have piece just to force the connection to stay open, for fun ?

        if piece_finished:
            # finished this piece
            #self.enqueue_message('HAVE', struct.pack('>I', index))
            conns = Connection.get_by_hash(self.request.connection.torrent.hash)
            for conn in conns:
                conn.send_message('HAVE', struct.pack('>I', index))
        if False:
            logging.info('got piece %s %s %s (q: %s, o: %s)' % (index, offset, len(data),
                                                                self.request.connection._piece_request_queued,
                                                                self.request.connection._piece_request_outbound,
                                                                ))
        self.finish()
