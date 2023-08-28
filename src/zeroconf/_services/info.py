""" Multicast DNS Service Discovery for Python, v0.14-wmcbrine
    Copyright 2003 Paul Scott-Murphy, 2014 William McBrine

    This module provides a framework for the use of DNS Service Discovery
    using IP multicast.

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301
    USA
"""

import asyncio
import random
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address, _BaseAddress, ip_address
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Union, cast

from .._dns import (
    DNSAddress,
    DNSNsec,
    DNSPointer,
    DNSQuestionType,
    DNSRecord,
    DNSService,
    DNSText,
)
from .._exceptions import BadTypeInNameException
from .._logger import log
from .._protocol.outgoing import DNSOutgoing
from .._updates import RecordUpdate, RecordUpdateListener
from .._utils.asyncio import (
    _resolve_all_futures_to_none,
    get_running_loop,
    run_coro_with_timeout,
    wait_for_future_set_or_timeout,
)
from .._utils.name import service_type_name
from .._utils.net import IPVersion, _encode_address
from .._utils.time import current_time_millis
from ..const import (
    _ADDRESS_RECORD_TYPES,
    _CLASS_IN,
    _CLASS_IN_UNIQUE,
    _DNS_HOST_TTL,
    _DNS_OTHER_TTL,
    _FLAGS_QR_QUERY,
    _LISTENER_TIME,
    _MDNS_PORT,
    _TYPE_A,
    _TYPE_AAAA,
    _TYPE_NSEC,
    _TYPE_PTR,
    _TYPE_SRV,
    _TYPE_TXT,
)

_IPVersion_All_value = IPVersion.All.value
_IPVersion_V4Only_value = IPVersion.V4Only.value
# https://datatracker.ietf.org/doc/html/rfc6762#section-5.2
# The most common case for calling ServiceInfo is from a
# ServiceBrowser. After the first request we add a few random
# milliseconds to the delay between requests to reduce the chance
# that there are multiple ServiceBrowser callbacks running on
# the network that are firing at the same time when they
# see the same multicast response and decide to refresh
# the A/AAAA/SRV records for a host.
_AVOID_SYNC_DELAY_RANDOM_INTERVAL = (20, 120)

if TYPE_CHECKING:
    from .._core import Zeroconf


def instance_name_from_service_info(info: "ServiceInfo", strict: bool = True) -> str:
    """Calculate the instance name from the ServiceInfo."""
    # This is kind of funky because of the subtype based tests
    # need to make subtypes a first class citizen
    service_name = service_type_name(info.name, strict=strict)
    if not info.type.endswith(service_name):
        raise BadTypeInNameException
    return info.name[: -len(service_name) - 1]


_cached_ip_addresses = lru_cache(maxsize=256)(ip_address)


class ServiceInfo(RecordUpdateListener):
    """Service information.

    Constructor parameters are as follows:

    * `type_`: fully qualified service type name
    * `name`: fully qualified service name
    * `port`: port that the service runs on
    * `weight`: weight of the service
    * `priority`: priority of the service
    * `properties`: dictionary of properties (or a bytes object holding the contents of the `text` field).
      converted to str and then encoded to bytes using UTF-8. Keys with `None` values are converted to
      value-less attributes.
    * `server`: fully qualified name for service host (defaults to name)
    * `host_ttl`: ttl used for A/SRV records
    * `other_ttl`: ttl used for PTR/TXT records
    * `addresses` and `parsed_addresses`: List of IP addresses (either as bytes, network byte order,
      or in parsed form as text; at most one of those parameters can be provided)
    * interface_index: scope_id or zone_id for IPv6 link-local addresses i.e. an identifier of the interface
      where the peer is connected to
    """

    __slots__ = (
        "text",
        "type",
        "_name",
        "key",
        "_ipv4_addresses",
        "_ipv6_addresses",
        "port",
        "weight",
        "priority",
        "server",
        "server_key",
        "_properties",
        "host_ttl",
        "other_ttl",
        "interface_index",
        "_new_records_futures",
    )

    def __init__(
        self,
        type_: str,
        name: str,
        port: Optional[int] = None,
        weight: int = 0,
        priority: int = 0,
        properties: Union[bytes, Dict] = b'',
        server: Optional[str] = None,
        host_ttl: int = _DNS_HOST_TTL,
        other_ttl: int = _DNS_OTHER_TTL,
        *,
        addresses: Optional[List[bytes]] = None,
        parsed_addresses: Optional[List[str]] = None,
        interface_index: Optional[int] = None,
    ) -> None:
        # Accept both none, or one, but not both.
        if addresses is not None and parsed_addresses is not None:
            raise TypeError("addresses and parsed_addresses cannot be provided together")
        if not type_.endswith(service_type_name(name, strict=False)):
            raise BadTypeInNameException
        self.text = b''
        self.type = type_
        self._name = name
        self.key = name.lower()
        self._ipv4_addresses: List[IPv4Address] = []
        self._ipv6_addresses: List[IPv6Address] = []
        if addresses is not None:
            self.addresses = addresses
        elif parsed_addresses is not None:
            self.addresses = [_encode_address(a) for a in parsed_addresses]
        self.port = port
        self.weight = weight
        self.priority = priority
        self.server = server if server else None
        self.server_key = server.lower() if server else None
        self._properties: Optional[Dict[Union[str, bytes], Optional[Union[str, bytes]]]] = None
        if isinstance(properties, bytes):
            self._set_text(properties)
        else:
            self._set_properties(properties)
        self.host_ttl = host_ttl
        self.other_ttl = other_ttl
        self.interface_index = interface_index
        self._new_records_futures: Set[asyncio.Future] = set()

    @property
    def name(self) -> str:
        """The name of the service."""
        return self._name

    @name.setter
    def name(self, name: str) -> None:
        """Replace the the name and reset the key."""
        self._name = name
        self.key = name.lower()

    @property
    def addresses(self) -> List[bytes]:
        """IPv4 addresses of this service.

        Only IPv4 addresses are returned for backward compatibility.
        Use :meth:`addresses_by_version` or :meth:`parsed_addresses` to
        include IPv6 addresses as well.
        """
        return self.addresses_by_version(IPVersion.V4Only)

    @addresses.setter
    def addresses(self, value: List[bytes]) -> None:
        """Replace the addresses list.

        This replaces all currently stored addresses, both IPv4 and IPv6.
        """
        self._ipv4_addresses.clear()
        self._ipv6_addresses.clear()

        for address in value:
            try:
                addr = _cached_ip_addresses(address)
            except ValueError:
                raise TypeError(
                    "Addresses must either be IPv4 or IPv6 strings, bytes, or integers;"
                    f" got {address!r}. Hint: convert string addresses with socket.inet_pton"
                )
            if addr.version == 4:
                self._ipv4_addresses.append(addr)
            else:
                self._ipv6_addresses.append(addr)

    @property
    def properties(self) -> Dict[Union[str, bytes], Optional[Union[str, bytes]]]:
        """If properties were set in the constructor this property returns the original dictionary
        of type `Dict[Union[bytes, str], Any]`.

        If properties are coming from the network, after decoding a TXT record, the keys are always
        bytes and the values are either bytes, if there was a value, even empty, or `None`, if there
        was none. No further decoding is attempted. The type returned is `Dict[bytes, Optional[bytes]]`.
        """
        if self._properties is None:
            self._unpack_text_into_properties()
        if TYPE_CHECKING:
            assert self._properties is not None
        return self._properties

    async def async_wait(self, timeout: float) -> None:
        """Calling task waits for a given number of milliseconds or until notified."""
        loop = get_running_loop()
        assert loop is not None
        await wait_for_future_set_or_timeout(loop, self._new_records_futures, timeout)

    def addresses_by_version(self, version: IPVersion) -> List[bytes]:
        """List addresses matching IP version.

        Addresses are guaranteed to be returned in LIFO (last in, first out)
        order with IPv4 addresses first and IPv6 addresses second.

        This means the first address will always be the most recently added
        address of the given IP version.
        """
        version_value = version.value
        if version_value == _IPVersion_All_value:
            return [
                *(addr.packed for addr in self._ipv4_addresses),
                *(addr.packed for addr in self._ipv6_addresses),
            ]
        if version_value == _IPVersion_V4Only_value:
            return [addr.packed for addr in self._ipv4_addresses]
        return [addr.packed for addr in self._ipv6_addresses]

    def ip_addresses_by_version(
        self, version: IPVersion
    ) -> Union[List[IPv4Address], List[IPv6Address], List[_BaseAddress]]:
        """List ip_address objects matching IP version.

        Addresses are guaranteed to be returned in LIFO (last in, first out)
        order with IPv4 addresses first and IPv6 addresses second.

        This means the first address will always be the most recently added
        address of the given IP version.
        """
        return self._ip_addresses_by_version_value(version.value)

    def _ip_addresses_by_version_value(
        self, version_value: int
    ) -> Union[List[IPv4Address], List[IPv6Address], List[_BaseAddress]]:
        """Backend for addresses_by_version that uses the raw value."""
        if version_value == _IPVersion_All_value:
            return [*self._ipv4_addresses, *self._ipv6_addresses]
        if version_value == _IPVersion_V4Only_value:
            return self._ipv4_addresses
        return self._ipv6_addresses

    def parsed_addresses(self, version: IPVersion = IPVersion.All) -> List[str]:
        """List addresses in their parsed string form.

        Addresses are guaranteed to be returned in LIFO (last in, first out)
        order with IPv4 addresses first and IPv6 addresses second.

        This means the first address will always be the most recently added
        address of the given IP version.
        """
        return [str(addr) for addr in self._ip_addresses_by_version_value(version.value)]

    def parsed_scoped_addresses(self, version: IPVersion = IPVersion.All) -> List[str]:
        """Equivalent to parsed_addresses, with the exception that IPv6 Link-Local
        addresses are qualified with %<interface_index> when available

        Addresses are guaranteed to be returned in LIFO (last in, first out)
        order with IPv4 addresses first and IPv6 addresses second.

        This means the first address will always be the most recently added
        address of the given IP version.
        """
        if self.interface_index is None:
            return self.parsed_addresses(version)
        return [
            f"{addr}%{self.interface_index}" if addr.version == 6 and addr.is_link_local else str(addr)
            for addr in self._ip_addresses_by_version_value(version.value)
        ]

    def _set_properties(self, properties: Dict[Union[str, bytes], Optional[Union[str, bytes]]]) -> None:
        """Sets properties and text of this info from a dictionary"""
        self._properties = properties
        list_: List[bytes] = []
        result = b''
        for key, value in properties.items():
            if isinstance(key, str):
                key = key.encode('utf-8')

            record = key
            if value is not None:
                if not isinstance(value, bytes):
                    value = str(value).encode('utf-8')
                record += b'=' + value
            list_.append(record)
        for item in list_:
            result = b''.join((result, bytes((len(item),)), item))
        self.text = result

    def _set_text(self, text: bytes) -> None:
        """Sets properties and text given a text field"""
        if text == self.text:
            return
        self.text = text
        # Clear the properties cache
        self._properties = None

    def _unpack_text_into_properties(self) -> None:
        """Unpacks the text field into properties"""
        text = self.text
        if not text:
            # Properties should be set atomically
            # in case another thread is reading them
            self._properties = {}
            return

        index = 0
        pairs: List[bytes] = []
        end = len(text)
        while index < end:
            length = text[index]
            index += 1
            pairs.append(text[index : index + length])
            index += length

        # Reverse the list so that the first item in the list
        # is the last item in the text field. This is important
        # to preserve backwards compatibility where the first
        # key always wins if the key is seen multiple times.
        pairs.reverse()
        self._properties = {key: value or None for key, _, value in (pair.partition(b'=') for pair in pairs)}

    def get_name(self) -> str:
        """Name accessor"""
        return self._name[: len(self._name) - len(self.type) - 1]

    def _get_ip_addresses_from_cache_lifo(
        self, zc: 'Zeroconf', now: float, type: int
    ) -> List[Union[IPv4Address, IPv6Address]]:
        """Set IPv6 addresses from the cache."""
        address_list: List[Union[IPv4Address, IPv6Address]] = []
        for record in self._get_address_records_from_cache_by_type(zc, type):
            if record.is_expired(now):
                continue
            try:
                ip_addr = _cached_ip_addresses(record.address)
            except ValueError:
                continue
            else:
                address_list.append(ip_addr)
        address_list.reverse()  # Reverse to get LIFO order
        return address_list

    def _set_ipv6_addresses_from_cache(self, zc: 'Zeroconf', now: float) -> None:
        """Set IPv6 addresses from the cache."""
        if TYPE_CHECKING:
            self._ipv6_addresses = cast(
                "List[IPv6Address]", self._get_ip_addresses_from_cache_lifo(zc, now, _TYPE_AAAA)
            )
        else:
            self._ipv6_addresses = self._get_ip_addresses_from_cache_lifo(zc, now, _TYPE_AAAA)

    def _set_ipv4_addresses_from_cache(self, zc: 'Zeroconf', now: float) -> None:
        """Set IPv4 addresses from the cache."""
        if TYPE_CHECKING:
            self._ipv4_addresses = cast(
                "List[IPv4Address]", self._get_ip_addresses_from_cache_lifo(zc, now, _TYPE_A)
            )
        else:
            self._ipv4_addresses = self._get_ip_addresses_from_cache_lifo(zc, now, _TYPE_A)

    def async_update_records(self, zc: 'Zeroconf', now: float, records: List[RecordUpdate]) -> None:
        """Updates service information from a DNS record.

        This method will be run in the event loop.
        """
        new_records_futures = self._new_records_futures
        updated: bool = False
        for record_update in records:
            updated |= self._process_record_threadsafe(zc, record_update.new, now)
        if updated and new_records_futures:
            _resolve_all_futures_to_none(new_records_futures)

    def _process_record_threadsafe(self, zc: 'Zeroconf', record: DNSRecord, now: float) -> bool:
        """Thread safe record updating.

        Returns True if a new record was added.
        """
        if record.is_expired(now):
            return False

        record_key = record.key
        if record_key == self.server_key and type(record) is DNSAddress:
            try:
                ip_addr = _cached_ip_addresses(record.address)
            except ValueError as ex:
                log.warning("Encountered invalid address while processing %s: %s", record, ex)
                return False

            if type(ip_addr) is IPv4Address:
                if self._ipv4_addresses:
                    self._set_ipv4_addresses_from_cache(zc, now)

                ipv4_addresses = self._ipv4_addresses
                if ip_addr not in ipv4_addresses:
                    ipv4_addresses.insert(0, ip_addr)
                    return True
                elif ip_addr != ipv4_addresses[0]:
                    ipv4_addresses.remove(ip_addr)
                    ipv4_addresses.insert(0, ip_addr)

                return False

            if not self._ipv6_addresses:
                self._set_ipv6_addresses_from_cache(zc, now)

            ipv6_addresses = self._ipv6_addresses
            if ip_addr not in self._ipv6_addresses:
                ipv6_addresses.insert(0, ip_addr)
                return True
            elif ip_addr != self._ipv6_addresses[0]:
                ipv6_addresses.remove(ip_addr)
                ipv6_addresses.insert(0, ip_addr)

            return False

        if record_key != self.key:
            return False

        if record.type == _TYPE_TXT and type(record) is DNSText:
            self._set_text(record.text)
            return True

        if record.type == _TYPE_SRV and type(record) is DNSService:
            old_server_key = self.server_key
            self.name = record.name
            self.server = record.server
            self.server_key = record.server_key
            self.port = record.port
            self.weight = record.weight
            self.priority = record.priority
            if old_server_key != self.server_key:
                self._set_ipv4_addresses_from_cache(zc, now)
                self._set_ipv6_addresses_from_cache(zc, now)
            return True

        return False

    def dns_addresses(
        self,
        override_ttl: Optional[int] = None,
        version: IPVersion = IPVersion.All,
        created: Optional[float] = None,
    ) -> List[DNSAddress]:
        """Return matching DNSAddress from ServiceInfo."""
        name = self.server or self._name
        ttl = override_ttl if override_ttl is not None else self.host_ttl
        class_ = _CLASS_IN_UNIQUE
        version_value = version.value
        return [
            DNSAddress(
                name,
                _TYPE_AAAA if type(ip_addr) is IPv6Address else _TYPE_A,
                class_,
                ttl,
                ip_addr.packed,
                created=created,
            )
            for ip_addr in self._ip_addresses_by_version_value(version_value)
        ]

    def dns_pointer(self, override_ttl: Optional[int] = None, created: Optional[float] = None) -> DNSPointer:
        """Return DNSPointer from ServiceInfo."""
        return DNSPointer(
            self.type,
            _TYPE_PTR,
            _CLASS_IN,
            override_ttl if override_ttl is not None else self.other_ttl,
            self._name,
            created,
        )

    def dns_service(self, override_ttl: Optional[int] = None, created: Optional[float] = None) -> DNSService:
        """Return DNSService from ServiceInfo."""
        port = self.port
        if TYPE_CHECKING:
            assert isinstance(port, int)
        return DNSService(
            self._name,
            _TYPE_SRV,
            _CLASS_IN_UNIQUE,
            override_ttl if override_ttl is not None else self.host_ttl,
            self.priority,
            self.weight,
            port,
            self.server or self._name,
            created,
        )

    def dns_text(self, override_ttl: Optional[int] = None, created: Optional[float] = None) -> DNSText:
        """Return DNSText from ServiceInfo."""
        return DNSText(
            self._name,
            _TYPE_TXT,
            _CLASS_IN_UNIQUE,
            override_ttl if override_ttl is not None else self.other_ttl,
            self.text,
            created,
        )

    def dns_nsec(
        self, missing_types: List[int], override_ttl: Optional[int] = None, created: Optional[float] = None
    ) -> DNSNsec:
        """Return DNSNsec from ServiceInfo."""
        return DNSNsec(
            self._name,
            _TYPE_NSEC,
            _CLASS_IN_UNIQUE,
            override_ttl if override_ttl is not None else self.host_ttl,
            self._name,
            missing_types,
            created,
        )

    def get_address_and_nsec_records(
        self, override_ttl: Optional[int] = None, created: Optional[float] = None
    ) -> Set[DNSRecord]:
        """Build a set of address records and NSEC records for non-present record types."""
        missing_types: Set[int] = _ADDRESS_RECORD_TYPES.copy()
        records: Set[DNSRecord] = set()
        for dns_address in self.dns_addresses(override_ttl, IPVersion.All, created):
            missing_types.discard(dns_address.type)
            records.add(dns_address)
        if missing_types:
            assert self.server is not None, "Service server must be set for NSEC record."
            records.add(self.dns_nsec(list(missing_types), override_ttl, created))
        return records

    def _get_address_records_from_cache_by_type(self, zc: 'Zeroconf', _type: int) -> List[DNSAddress]:
        """Get the addresses from the cache."""
        if self.server_key is None:
            return []
        return cast("List[DNSAddress]", zc.cache.get_all_by_details(self.server_key, _type, _CLASS_IN))

    def set_server_if_missing(self) -> None:
        """Set the server if it is missing.

        This function is for backwards compatibility.
        """
        if self.server is None:
            self.server = self._name
            self.server_key = self.key

    def load_from_cache(self, zc: 'Zeroconf', now: Optional[float] = None) -> bool:
        """Populate the service info from the cache.

        This method is designed to be threadsafe.
        """
        if not now:
            now = current_time_millis()
        original_server_key = self.server_key
        cached_srv_record = zc.cache.get_by_details(self._name, _TYPE_SRV, _CLASS_IN)
        if cached_srv_record:
            self._process_record_threadsafe(zc, cached_srv_record, now)
        cached_txt_record = zc.cache.get_by_details(self._name, _TYPE_TXT, _CLASS_IN)
        if cached_txt_record:
            self._process_record_threadsafe(zc, cached_txt_record, now)
        if original_server_key == self.server_key:
            # If there is a srv which changes the server_key,
            # A and AAAA will already be loaded from the cache
            # and we do not want to do it twice
            for record in [
                *self._get_address_records_from_cache_by_type(zc, _TYPE_A),
                *self._get_address_records_from_cache_by_type(zc, _TYPE_AAAA),
            ]:
                self._process_record_threadsafe(zc, record, now)
        return self._is_complete

    @property
    def _is_complete(self) -> bool:
        """The ServiceInfo has all expected properties."""
        return bool(self.text is not None and (self._ipv4_addresses or self._ipv6_addresses))

    def request(
        self,
        zc: 'Zeroconf',
        timeout: float,
        question_type: Optional[DNSQuestionType] = None,
        addr: Optional[str] = None,
        port: int = _MDNS_PORT,
    ) -> bool:
        """Returns true if the service could be discovered on the
        network, and updates this object with details discovered.

        While it is not expected during normal operation,
        this function may raise EventLoopBlocked if the underlying
        call to `async_request` cannot be completed.
        """
        assert zc.loop is not None and zc.loop.is_running()
        if zc.loop == get_running_loop():
            raise RuntimeError("Use AsyncServiceInfo.async_request from the event loop")
        return bool(
            run_coro_with_timeout(
                self.async_request(zc, timeout, question_type, addr, port), zc.loop, timeout
            )
        )

    async def async_request(
        self,
        zc: 'Zeroconf',
        timeout: float,
        question_type: Optional[DNSQuestionType] = None,
        addr: Optional[str] = None,
        port: int = _MDNS_PORT,
    ) -> bool:
        """Returns true if the service could be discovered on the
        network, and updates this object with details discovered.

        This method will be run in the event loop.

        Passing addr and port is optional, and will default to the
        mDNS multicast address and port. This is useful for directing
        requests to a specific host that may be able to respond across
        subnets.
        """
        if not zc.started:
            await zc.async_wait_for_start()

        now = current_time_millis()

        if self.load_from_cache(zc, now):
            return True

        first_request = True
        delay = _LISTENER_TIME
        next_ = now
        last = now + timeout
        try:
            zc.async_add_listener(self, None)
            while not self._is_complete:
                if last <= now:
                    return False
                if next_ <= now:
                    out = self.generate_request_query(
                        zc, now, question_type or DNSQuestionType.QU if first_request else DNSQuestionType.QM
                    )
                    first_request = False
                    if not out.questions:
                        return self.load_from_cache(zc, now)
                    zc.async_send(out, addr, port)
                    next_ = now + delay
                    delay *= 2
                    next_ += random.randint(*_AVOID_SYNC_DELAY_RANDOM_INTERVAL)

                await self.async_wait(min(next_, last) - now)
                now = current_time_millis()
        finally:
            zc.async_remove_listener(self)

        return True

    def generate_request_query(
        self, zc: 'Zeroconf', now: float, question_type: Optional[DNSQuestionType] = None
    ) -> DNSOutgoing:
        """Generate the request query."""
        out = DNSOutgoing(_FLAGS_QR_QUERY)
        name = self._name
        server_or_name = self.server or name
        cache = zc.cache
        out.add_question_or_one_cache(cache, now, name, _TYPE_SRV, _CLASS_IN)
        out.add_question_or_one_cache(cache, now, name, _TYPE_TXT, _CLASS_IN)
        out.add_question_or_all_cache(cache, now, server_or_name, _TYPE_A, _CLASS_IN)
        out.add_question_or_all_cache(cache, now, server_or_name, _TYPE_AAAA, _CLASS_IN)
        if question_type == DNSQuestionType.QU:
            for question in out.questions:
                question.unicast = True
        return out

    def __eq__(self, other: object) -> bool:
        """Tests equality of service name"""
        return isinstance(other, ServiceInfo) and other._name == self._name

    def __repr__(self) -> str:
        """String representation"""
        return '{}({})'.format(
            type(self).__name__,
            ', '.join(
                f'{name}={getattr(self, name)!r}'
                for name in (
                    'type',
                    'name',
                    'addresses',
                    'port',
                    'weight',
                    'priority',
                    'server',
                    'properties',
                    'interface_index',
                )
            ),
        )
