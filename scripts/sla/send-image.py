#!/usr/bin/env python3
"""
Send Image Command Client
Sends JSON commands to the image display server via TCP.
"""

from __future__ import annotations

import socket
import json
import os
import subprocess
import sys
import argparse
import time

DEFAULT_SERVICE_NAME = 'bioslicer-image-display.service'


def _is_local_host(host: str) -> bool:
    return host in {'localhost', '127.0.0.1', '::1'}


def _systemctl_available() -> bool:
    try:
        result = subprocess.run(['systemctl', '--version'], capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0


def _systemctl_cmd(user_mode: bool, *args: str) -> list[str]:
    cmd = ['systemctl']
    if user_mode:
        cmd.append('--user')
    cmd.extend(args)
    return cmd


def _service_active(service_name: str, user_mode: bool) -> bool:
    result = subprocess.run(
        _systemctl_cmd(user_mode, 'is-active', '--quiet', service_name),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _start_service(service_name: str, user_mode: bool) -> tuple[bool, str]:
    result = subprocess.run(
        _systemctl_cmd(user_mode, '--no-ask-password', 'start', service_name),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ''

    detail = result.stderr.strip() or result.stdout.strip() or 'unknown error'
    return False, detail


def ensure_service_running(service_name: str) -> tuple[bool, str]:
    """Ensure the image display service is running via systemd if available."""
    if not service_name:
        return False, 'Service name is empty'
    if not _systemctl_available():
        return False, 'systemctl not available on this host'

    attempts = []
    modes = [('system', False)]
    if os.environ.get('XDG_RUNTIME_DIR'):
        modes.insert(0, ('user', True))

    for mode_name, user_mode in modes:
        if _service_active(service_name, user_mode):
            return True, f'{service_name} already active ({mode_name} mode)'

        started, detail = _start_service(service_name, user_mode)
        if started and _service_active(service_name, user_mode):
            return True, f'{service_name} started ({mode_name} mode)'

        if detail:
            attempts.append(f'{mode_name}: {detail}')
        else:
            attempts.append(f'{mode_name}: failed to start')

    return False, '; '.join(attempts)


def _send_once(host: str, port: int, command: dict, timeout_sec: float = 3.0) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_sec)
        sock.connect((host, port))

        command_json = json.dumps(command) + '\n'
        sock.sendall(command_json.encode())

        with sock.makefile('r', encoding='utf-8') as stream:
            response_line = stream.readline()

    if not response_line:
        raise RuntimeError('No response from display server')

    return json.loads(response_line.strip())


def send_command(
    host: str,
    port: int,
    command: dict,
    ensure_running: bool = True,
    service_name: str = DEFAULT_SERVICE_NAME,
    start_timeout: float = 3.0,
) -> dict:
    """Send a command to the server and return the response."""
    try:
        return _send_once(host, port, command)

    except ConnectionRefusedError:
        if ensure_running and _is_local_host(host):
            started, detail = ensure_service_running(service_name)
            if not started:
                return {
                    'status': 'error',
                    'message': (
                        f'Connection refused on {host}:{port}. '
                        f'Autostart failed for {service_name}: {detail}'
                    )
                }

            deadline = time.time() + max(0.5, start_timeout)
            while time.time() <= deadline:
                try:
                    return _send_once(host, port, command)
                except ConnectionRefusedError:
                    time.sleep(0.2)

            return {
                'status': 'error',
                'message': (
                    f'{service_name} was started, but the display server '
                    f'did not accept connections on {host}:{port} yet'
                )
            }

        return {
            'status': 'error',
            'message': f'Connection refused. Is the server running on {host}:{port}?'
        }
    except socket.timeout:
        return {
            'status': 'error',
            'message': 'Connection timed out'
        }
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Error: {str(e)}'
        }

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Send commands to the image display server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
    python send-image.py localhost 5555 '{"type":"DISPLAY_IMAGE","path":"C:\\path\\to\\image.png","rotation":180}'
    python send-image.py localhost 5555 '{"type":"SHOW_VIDEO_FRAME","name":"resin_a","frame":240}'
    python send-image.py localhost 5555 '{"type":"CLEAR"}'
        """
    )
    
    parser.add_argument('host', help='Server hostname or IP address')
    parser.add_argument('port', type=int, help='Server port number')
    parser.add_argument('command', help='Command JSON payload')
    parser.add_argument(
        '--service-name',
        default=DEFAULT_SERVICE_NAME,
        help='Systemd service name to autostart when localhost connection is refused',
    )
    parser.add_argument(
        '--start-timeout',
        default=3.0,
        type=float,
        help='Seconds to wait for the server after an autostart attempt',
    )
    parser.add_argument(
        '--no-ensure-running',
        action='store_true',
        help='Do not try to autostart a local systemd service when connection is refused',
    )
    
    args = parser.parse_args()
    
    try:
        print(f"Connecting to {args.host}:{args.port}")

        command_payload = json.loads(args.command)
        print(f"Sending command: {json.dumps(command_payload, indent=2)}")
        
        # Send command and get response
        response = send_command(
            args.host,
            args.port,
            command_payload,
            ensure_running=not args.no_ensure_running,
            service_name=args.service_name,
            start_timeout=args.start_timeout,
        )
        
        # Display response
        print(f"\nResponse:")
        print(json.dumps(response, indent=2))
        
        # Exit with error code if command failed
        if response.get('status') == 'error':
            sys.exit(1)
            
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled", file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
