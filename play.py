"""Build and run Nightwatch with one isolated container per player.

Run this host-side helper from macOS, Linux, or Windows with Docker running.
The game's inter-user communication remains entirely inside comms.py.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = 'nightwatch-isolated'
DEFAULT_SESSION = 'nightwatch23'
LABEL = 'nightwatch.project=23'


def run(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True).stdout.strip()


def names(session):
    return session + '-moderator', session + '-socket', session + '-state'


def profile(memory='64m', cpus='0.25'):
    return ['--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--memory', memory,
            '--memory-swap', memory, '--cpus', cpus, '--pids-limit', '32',
            '--ulimit', 'nofile=128:128', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=8m',
            '--label', LABEL]


def prepare_volumes(session):
    _, socket_volume, state_volume = names(session)
    for volume in (socket_volume, state_volume):
        run('volume', 'create', '--label', LABEL, volume, stdout=subprocess.DEVNULL)
        metadata = json.loads(output('volume', 'inspect', volume))[0]
        if (metadata.get('Labels') or {}).get('nightwatch.project') != '23':
            raise RuntimeError('Refusing a volume not owned by this project: ' + volume)
    script = ("import os; "
              "os.chmod('/run/nightwatch',0o750); "
              "os.chmod('/var/lib/nightwatch',0o700); "
              "os.chown('/run/nightwatch',10000,2000); "
              "os.chown('/var/lib/nightwatch',10000,2000)")
    run('run', '--rm', '--network', 'none', '--read-only', '--user', '0',
        '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER',
        '--security-opt', 'no-new-privileges',
        '--label', LABEL, '-v', socket_volume + ':/run/nightwatch',
        '-v', state_volume + ':/var/lib/nightwatch', IMAGE, 'python3', '-c', script,
        stdout=subprocess.DEVNULL)


def ready(name):
    started = output('inspect', '-f', '{{.State.StartedAt}}', name)
    for _ in range(80):
        status = subprocess.run(['docker', 'logs', '--since', started, name], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if 'NIGHTWATCH moderator ready' in status.stdout:
            return
        state = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if state.stdout.strip() != 'true':
            raise RuntimeError('Moderator stopped:\n' + status.stdout)
        time.sleep(.25)
    raise RuntimeError('Moderator startup timed out; inspect docker logs ' + name)


def start(session=DEFAULT_SESSION, players=5, fast=False, seed=None, durations=None):
    server, socket_volume, state_volume = names(session)
    prepare_volumes(session)
    command = ['run', '-d', '--name', server, *profile('128m', '1'), '--user', '10000:2000',
               '-v', socket_volume + ':/run/nightwatch',
               '-v', state_volume + ':/var/lib/nightwatch', IMAGE, 'python3', 'server.py',
               '--players', ','.join(f'p{i}' for i in range(1, players+1))]
    if fast:
        durations = (2, 2, 2)
    if durations:
        for flag, value in zip(('--night','--discussion','--vote'), durations):
            command += [flag, str(value)]
    if seed is not None:
        command += ['--seed', str(seed)]
    run(*command, stdout=subprocess.DEVNULL)
    ready(server)
    print(f'Moderator ready: {server}. Checkpoints stay in volume {state_volume}.', flush=True)


def client_command(session, user, bot=False, interactive=False, remove=True):
    number = int(user[1:])
    _, socket_volume, _ = names(session)
    command = ['run', '--name', session + '-' + user, *profile(), '--user', f'{10000+number}:2000',
               '-v', socket_volume + ':/run/nightwatch:ro']
    if remove:
        command += ['--rm']
    if interactive:
        command += ['-it']
    command += [IMAGE, 'python3', 'client.py']
    if bot:
        command += ['--bot']
    return command


def managed_containers(session):
    listed = output('ps', '-a', '--filter', 'label=' + LABEL, '--format', '{{.Names}}')
    return [name for name in listed.splitlines() if name.startswith(session + '-')]


def stop(session, purge=False):
    containers = managed_containers(session)
    if containers:
        if purge:
            run('rm', '-f', *containers, stdout=subprocess.DEVNULL)
        else:
            run('stop', *containers, stdout=subprocess.DEVNULL)
    if purge:
        for volume in names(session)[1:]:
            inspected = subprocess.run(['docker','volume','inspect',volume], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
            if inspected.returncode == 0:
                metadata = json.loads(inspected.stdout)[0]
                if (metadata.get('Labels') or {}).get('nightwatch.project') != '23':
                    raise RuntimeError('Refusing a volume not owned by this project: ' + volume)
                run('volume','rm',volume,stdout=subprocess.DEVNULL)
    print('Stopped ' + session + ('. Its disposable volumes were removed.' if purge else '. Saved match volumes are retained.'))


def resume(session):
    server = names(session)[0]
    run('start', server, stdout=subprocess.DEVNULL)
    ready(server)
    print('Saved match resumed. Rejoin with the same player names.', flush=True)


def demo(players, destination=None, keep=False):
    session = 'nightwatch23-demo-' + str(time.time_ns())
    destination = Path(destination) if destination else ROOT / 'evidence' / 'isolated-demo'
    destination.mkdir(parents=True, exist_ok=True)
    processes, handles = [], []
    try:
        start(session, players, fast=True, seed=7)
        for number in range(1,players+1):
            user = f'p{number}'
            handle = (destination / (user+'.txt')).open('w')
            handles.append(handle)
            processes.append(subprocess.Popen(['docker', *client_command(session,user,bot=True,remove=False)],
                                               stdout=handle,stderr=subprocess.STDOUT))
        deadline = time.monotonic()+150
        for process in processes:
            process.wait(timeout=max(1,deadline-time.monotonic()))
            if process.returncode:
                raise RuntimeError('A client failed. Inspect ' + str(destination))
        for handle in handles:
            handle.flush()
        with (destination/'moderator.txt').open('w') as handle:
            run('logs', names(session)[0],stdout=handle,stderr=subprocess.STDOUT)
        records=[]
        for number in range(1,players+1):
            user=f'p{number}';lines=(destination/(user+'.txt')).read_text().splitlines()
            identity=next(line for line in lines if line.startswith('IDENTITY:'))
            namespace=next(line for line in lines if line.startswith('PID namespace:'))
            result=next((line for line in lines if 'WIN!' in line),None)
            if result is None:
                raise RuntimeError('The demonstration did not finish for '+user)
            config=json.loads(output('inspect',session+'-'+user))[0]
            records.append({'user':user,'identity':identity,'pid_namespace':namespace,'result':result,
                            'container_id':config['Id'],'user_config':config['Config']['User'],
                            'limits':{k:config['HostConfig'][k] for k in ['Memory','MemorySwap','NanoCpus','PidsLimit','ReadonlyRootfs','NetworkMode','CapDrop','SecurityOpt']}})
        assert len({r['pid_namespace'] for r in records})==players
        assert len({r['user_config'] for r in records})==players
        (destination/'summary.json').write_text(json.dumps(records,indent=2)+'\n')
        print('COMPLETED: separate player containers, Linux UIDs, and PID namespaces.')
        for record in records:
            print(record['identity']+' | '+record['pid_namespace'])
        print(records[0]['result'])
        print('Actual transcripts: '+str(destination.resolve()))
        return session
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for handle in handles:
            handle.close()
        if not keep:
            stop(session,purge=True)
        else:
            print('Evidence containers retained for screenshots: '+session)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['build','start','join','demo','test','stop','resume'])
    parser.add_argument('user',nargs='?')
    parser.add_argument('--session',default=DEFAULT_SESSION)
    parser.add_argument('--players',type=int,choices=[5,6,7],default=5)
    parser.add_argument('--purge',action='store_true',help='Also delete this session\'s saved match; use only for a new game')
    parser.add_argument('--keep',action='store_true',help='Retain demo containers and volumes for inspecting logs')
    parser.add_argument('--evidence',help='Destination directory for demo transcripts')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,55}',args.session):
        parser.error('Use a session name of 1-56 lowercase letters, digits, or hyphens')
    if args.action=='build':
        run('build','-t',IMAGE,str(ROOT))
    elif args.action=='start':
        start(args.session,args.players)
        print('One terminal per player: python3 play.py join p1 (then p2 through p5).')
    elif args.action=='join':
        if args.user not in [f'p{i}' for i in range(1,8)]:
            parser.error('Choose p1 through p7')
        run(*client_command(args.session,args.user,interactive=True))
    elif args.action=='demo':
        demo(args.players,args.evidence,args.keep)
    elif args.action=='test':
        run('run','--rm',*profile('128m','1'),IMAGE,'python3','-m','unittest','-v','tests')
    elif args.action=='stop':
        stop(args.session,args.purge)
    elif args.action=='resume':
        resume(args.session)


if __name__=='__main__':
    try:
        main()
    except (subprocess.CalledProcessError,RuntimeError,subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
