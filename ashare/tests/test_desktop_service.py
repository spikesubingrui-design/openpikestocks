import plistlib
import socket
from types import SimpleNamespace
import pytest
from ashare import service


@pytest.fixture
def sandbox_service(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'; data = root / '.local/ashare'
    (data / 'venv/bin').mkdir(parents=True)
    (data / 'venv/bin/python').write_text('fixture')
    monkeypatch.setattr(service, 'ROOT', root)
    monkeypatch.setattr(service, 'DATA', data)
    monkeypatch.setattr(service, 'PLIST', tmp_path / 'agent.plist')
    monkeypatch.setattr(service.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    calls = []
    monkeypatch.setattr(service, 'command', lambda args: calls.append(args))
    return calls


def test_install_does_not_replace_port_owner(sandbox_service, monkeypatch):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen()
        monkeypatch.setattr(service, 'PORT', listener.getsockname()[1])
        with pytest.raises(RuntimeError, match='已被占用'):
            service.install()
    assert not sandbox_service and not service.PLIST.exists()


def test_recent_closed_connection_does_not_block_restart(sandbox_service, monkeypatch):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0)); listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port)) as client:
            conn, _ = listener.accept()
            conn.shutdown(socket.SHUT_RDWR); conn.close()
            assert client.recv(1) == b''
    monkeypatch.setattr(service, 'PORT', port)
    assert service.install()['installed']
    assert len(sandbox_service) == 1 and sandbox_service[0][1] == 'bootstrap'


def test_loaded_service_belongs_to_another_workspace(sandbox_service, monkeypatch):
    monkeypatch.setattr(service.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    service.PLIST.write_bytes(plistlib.dumps({'WorkingDirectory': '/other/workspace'}))
    before = service.PLIST.read_bytes()
    with pytest.raises(RuntimeError, match='不属于本工程'):
        service.install()
    assert service.PLIST.read_bytes() == before and not sandbox_service


def test_second_launch_reuses_existing_service(sandbox_service, monkeypatch):
    monkeypatch.setattr(service.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    service.PLIST.write_bytes(plistlib.dumps({'WorkingDirectory': str(service.ROOT)}))
    assert service.install()['already_running']
    assert not sandbox_service
