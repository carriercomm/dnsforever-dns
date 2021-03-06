
import os
import json

from collections import defaultdict

from twisted.internet import defer
from twisted.names import dns, common, error
from twisted.python import failure
from twisted.web.client import getPage


class DnsforeverAuthority(common.ResolverBase):
    """
    An Authority that is loaded from a master server.

    @ivar _ADDITIONAL_PROCESSING_TYPES: Record types for which additional
        processing will be done.
    @ivar _ADDRESS_TYPES: Record types which are useful for inclusion in the
        additional section generated during additional processing.
    """
    # See https://twistedmatrix.com/trac/ticket/6650
    _ADDITIONAL_PROCESSING_TYPES = (dns.CNAME, dns.MX, dns.NS)
    _ADDRESS_TYPES = (dns.A, dns.AAAA)

    zones = None

    def __init__(self, serverAddr):
        common.ResolverBase.__init__(self)
        self.serverAddr = serverAddr
        self.zones = defaultdict(lambda: None)
        self.last_update = 0

    def update(self):
        def http_callback(data):
            data = json.loads(data)
            for (name, info) in data.items():
                self.last_update = info['last_update']
                self.delZone(name)
                for record in info['records']:
                    record = record.split(None, 2)
                    self.addRecord(name, 300, record[1], record[0], 'IN', record[2])

        page = getPage('http://%s/apis/server/update?last_update=%s' % (self.serverAddr, self.last_update))
        page.addCallbacks(callback=http_callback)

    @property
    def last_update(self):
        return self._last_update

    @last_update.setter
    def last_update(self, t):
        if self._last_update < t:
            self._last_update = t

    def _lookup_records(self, name):
        labels = name.lower().split('.')

        for i in range(len(labels), 0, -1):
            zone_name = '.'.join(labels[-i:])
            if self.zones[zone_name]:
                return self.zones[zone_name], name

        return None, name

    def _additionalRecords(self, answer, authority):
        """
        Find locally known information that could be useful to the consumer of
        the response and construct appropriate records to include in the
        I{additional} section of that response.

        Essentially, implement RFC 1034 section 4.3.2 step 6.

        @param answer: A L{list} of the records which will be included in the
            I{answer} section of the response.

        @param authority: A L{list} of the records which will be included in
            the I{authority} section of the response.

        @return: A generator of L{dns.RRHeader} instances for inclusion in the
            I{additional} section.  These instances represent extra information
            about the records in C{answer} and C{authority}.
        """
        for record in answer + authority:
            if record.type in self._ADDITIONAL_PROCESSING_TYPES:
                name = record.payload.name.name
                records, zone_name = self._lookup_records(name)
                if not records:
                    continue

                for rec in records.get(name.lower(), ()):
                    if rec.TYPE in self._ADDRESS_TYPES:
                        yield dns.RRHeader(
                            name, rec.TYPE, dns.IN,
                            rec.ttl, rec, auth=True)

    def _lookup(self, name, cls, type, timeout = None):
        """
        Determine a response to a particular DNS query.

        @param name: The name which is being queried and for which to lookup a
            response.
        @type name: L{bytes}

        @param cls: The class which is being queried.  Only I{IN} is
            implemented here and this value is presently disregarded.
        @type cls: L{int}

        @param type: The type of records being queried.  See the types defined
            in L{twisted.names.dns}.
        @type type: L{int}

        @param timeout: All processing is done locally and a result is
            available immediately, so the timeout value is ignored.

        @return: A L{Deferred} that fires with a L{tuple} of three sets of
            response records (to comprise the I{answer}, I{authority}, and
            I{additional} sections of a DNS response) or with a L{Failure} if
            there is a problem processing the query.
        """
        cnames = []
        results = []
        authority = []
        additional = []

        print '%s %s' % (name, dns.QUERY_TYPES[type])

        domain_zone, zone_name = self._lookup_records(name.lower())
        if not domain_zone:
            return defer.fail(failure.Failure(error.DomainError(name)))

        domain_records = domain_zone.get(name.lower())
        if not domain_records:
            return defer.fail(failure.Failure(dns.AuthoritativeDomainError(name)))

        for record in domain_records:
            ttl = record.ttl

            if record.TYPE == dns.NS and name.lower() != zone_name.lower():
                # NS record belong to a child zone: this is a referral.  As
                # NS records are authoritative in the child zone, ours here
                # are not.  RFC 2181, section 6.1.
                authority.append(
                    dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=False)
                )
            elif record.TYPE == type or type == dns.ALL_RECORDS:
                results.append(
                    dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=True)
                )
            if record.TYPE == dns.CNAME:
                cnames.append(
                    dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=True)
                )
            if record.TYPE == dns.SOA:
                soa = record
        if not results:
            results = cnames

        # https://tools.ietf.org/html/rfc1034#section-4.3.2 - sort of.
        # See https://twistedmatrix.com/trac/ticket/6732
        additionalInformation = self._additionalRecords(results, authority)
        if cnames:
            results.extend(additionalInformation)
        else:
            additional.extend(additionalInformation)

        if not results and not authority:
            # Empty response. Include SOA record to allow clients to cache
            # this response.  RFC 1034, sections 3.7 and 4.3.4, and RFC 2181
            # section 7.1.

            authority.append(
                dns.RRHeader(zone_name, dns.SOA, dns.IN, ttl, soa, auth=True)
                )
        return defer.succeed((results, authority, additional))

    def lookupZone(self, name, timeout = 10):
        name = name.lower()
        soa = None
        if self.zones[name]:
            for record in self.zones[name]:
                if isinstance(record, dns.SOA):
                    soa = record
                    break

        if not soa:
            return defer.fail(failure.Failure(dns.DomainError(name)))

        results = [dns.RRHeader(name, dns.SOA, dns.IN, soa.ttl, soa, auth=True)]
        for (k, r) in self.zones[name].items():
            for rec in r:
                if rec.TYPE != dns.SOA:
                    results.append(dns.RRHeader(k, rec.TYPE, dns.IN, rec.ttl, rec, auth=True))
        results.append(results[0])
        return defer.succeed((results, (), ()))

    def delZone(self, zone_name):
        if self.zones[zone_name]:
            del self.zones[zone_name]
            print 'Deleting zone', zone_name

    def addRecord(self, zone_name, ttl, type, subdomain, cls, rdata):
        if not self.zones[zone_name]:
            self.zones[zone_name] = {}

        if subdomain == '@':
            domain = zone_name
        else:
            domain = subdomain + '.' + zone_name

        record = getattr(dns, 'Record_%s' % type, None)
        if not record:
            raise NotImplementedError, "Record type %r not supported" % type

        if type in ['TXT']:
            rdata = [str(rdata)]
        else:
            rdata = str(rdata).split()

        r = record(*rdata)
        r.ttl = ttl

        self.zones[zone_name].setdefault(domain.lower(), []).append(r)

        print 'Adding IN Record', domain, ttl, r
