"""CI에서는 모의 전송과 loopback DB만 사용하고 외부 서비스 연결을 차단한다."""
import ipaddress
import os
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_test_network(monkeypatch):
    if os.getenv('SQ_TEST_OFFLINE')!='1':return
    connect,connect_ex=socket.socket.connect,socket.socket.connect_ex

    def validate(sock,address):
        if sock.family==socket.AF_UNIX:return
        host=address[0]
        try:allowed=ipaddress.ip_address(host).is_loopback
        except ValueError:allowed=host=='localhost'
        if not allowed:raise AssertionError('오프라인 검사에서 외부 네트워크 연결을 차단했습니다. MockTransport를 사용하세요.')

    def guarded_connect(sock,address):
        validate(sock,address)
        return connect(sock,address)

    def guarded_connect_ex(sock,address):
        validate(sock,address)
        return connect_ex(sock,address)

    monkeypatch.setattr(socket.socket,'connect',guarded_connect)
    monkeypatch.setattr(socket.socket,'connect_ex',guarded_connect_ex)
