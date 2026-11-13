"""Reproduce isolation and moderator-crash tests in disposable local sessions.

Host-side orchestration only. Driver processes use comms.py for all game IPC.
Run after build: python3 verify.py --evidence evidence/verification [--keep]
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import play

DRIVER = r'''
import asyncio,json,os,sys
from comms import Client
async def main():
    c=await Client.connect('/run/nightwatch/game.sock')
    print(json.dumps({'local_uid':os.getuid(),'local_pid':os.getpid(),'pid_namespace':os.readlink('/proc/self/ns/pid')}),flush=True)
    async def receive():
        try:
            while True:
                m=await c.receive();print(json.dumps(m),flush=True)
                if m.get('kind')=='result':return
        except (OSError,asyncio.IncompleteReadError):
            print(json.dumps({'local':'disconnected'}),flush=True)
    reader=asyncio.StreamReader()
    transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin)
    async def commands():
        while True:
            line=await reader.readline()
            if not line:return
            await c.send(json.loads(line))
    tasks=[asyncio.create_task(receive()),asyncio.create_task(commands())]
    try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        transport.close();await c.close()
asyncio.run(main())
'''

CPU = r'''
import json,time
from pathlib import Path
p=Path('/sys/fs/cgroup')
def usage():return int(dict(line.split() for line in (p/'cpu.stat').read_text().splitlines())['usage_usec'])
start=time.monotonic();first=usage();last_t=start;last_u=first;samples=[]
for i in range(10):
    end=start+(i+1)*.5
    while time.monotonic()<end:pass
    now=time.monotonic();u=usage()
    samples.append({'seconds':now-start,'cpu_percent_one_core':100*(u-last_u)/((now-last_t)*1e6)})
    last_t,last_u=now,u
print(json.dumps({'cpu_max':(p/'cpu.max').read_text().strip(),'cpu_stat':(p/'cpu.stat').read_text(),'mean_cpu_percent_one_core':100*(last_u-first)/((last_t-start)*1e6),'samples':samples}),flush=True)
'''
PIDS = r'''
import errno,json,os,signal,time
from pathlib import Path
children=[];limited=False
try:
    for i in range(48):
        try:pid=os.fork()
        except OSError as e:
            if e.errno==errno.EAGAIN:limited=True;break
            raise
        if pid==0:
            time.sleep(4);os._exit(0)
        children.append(pid)
    print(json.dumps({'children_started':len(children),'hit_eagain':limited,'pids_max':Path('/sys/fs/cgroup/pids.max').read_text().strip(),'pids_current':Path('/sys/fs/cgroup/pids.current').read_text().strip()}),flush=True)
finally:
    for pid in children:
        try:os.kill(pid,signal.SIGTERM)
        except ProcessLookupError:pass
    for pid in children:os.waitpid(pid,0)
'''
MEMORY = r'''
chunks=[]
for i in range(24):
    a=bytearray(8*1024*1024)
    for n in range(0,len(a),4096):a[n]=1
    chunks.append(a)
raise SystemExit('unexpectedly allocated 192 MiB despite the configured 64 MiB cap')
'''
PERMISSIONS = r'''
import json,os
checks={}
for name,fn in [
 ('read_only_root',lambda:open('/app/player-wrote.txt','w')),
 ('cannot_unlink_broker_socket',lambda:os.unlink('/run/nightwatch/game.sock')),
 ('cannot_read_moderator_checkpoint',lambda:open('/var/lib/nightwatch/checkpoint.json'))]:
    try:fn();checks[name]=False
    except OSError as e:checks[name]={'blocked':True,'errno':e.errno,'reason':str(e)}
checks['pid_namespace']=os.readlink('/proc/self/ns/pid')
checks['pid1_command']=open('/proc/1/cmdline','rb').read().decode().split(chr(0))[:3]
print(json.dumps(checks),flush=True)
'''

class Peer:
    def __init__(self,session,user,destination):
        self.name,self.container=user,session+'-'+user
        command=play.client_command(session,user,remove=False)
        index=command.index(play.IMAGE);command.insert(index,'-i')
        command=command[:index+2]+['python3','-u','-c',DRIVER]
        self.process=subprocess.Popen(['docker',*command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,text=True,bufsize=1)
        self.messages=[];self.condition=threading.Condition()
        self.log=(destination/(user+'.txt')).open('w')
        self.thread=threading.Thread(target=self._read,daemon=True);self.thread.start()
    def _read(self):
        for line in self.process.stdout:
            self.log.write(line);self.log.flush()
            try:message=json.loads(line)
            except ValueError:message={'driver_output':line.strip()}
            with self.condition:
                self.messages.append(message);self.condition.notify_all()
    def wait(self,predicate,after=0,timeout=20):
        end=time.monotonic()+timeout
        with self.condition:
            while True:
                found=next((m for m in self.messages[after:] if predicate(m)),None)
                if found is not None:return found
                if self.process.poll() is not None:
                    raise RuntimeError(self.container+' disconnected before the expected message: '+repr(self.messages[-3:]))
                remaining=end-time.monotonic()
                if remaining<=0:raise TimeoutError(self.container+' expected message missing')
                self.condition.wait(min(remaining,.25))
    def kind(self,kind,**kw):return self.wait(lambda m:m.get('kind')==kind,**kw)
    def send(self,message):
        mark=len(self.messages)
        self.process.stdin.write(json.dumps(message)+'\n');self.process.stdin.flush()
        return mark
    def status(self):
        mark=self.send({'cmd':'status'})
        return self.kind('state',after=mark,timeout=4)
    def command(self,cmd,phase_id,**kw):
        mark=self.send({'cmd':cmd,'phase_id':phase_id,**kw})
        result=self.wait(lambda m:m.get('kind') in ('ack','error'),after=mark,timeout=4)
        if result['kind']=='error':raise RuntimeError(result['text'])
        return result
    def close(self):
        if self.process.poll() is None:
            try:self.process.stdin.close()
            except OSError:pass
        try:self.process.wait(3)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(3)
        self.thread.join(2);self.log.close()


def exec_python(container,script,check=True):
    p=subprocess.run(['docker','exec',container,'python3','-c',script],stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT,text=True)
    if check and p.returncode:raise RuntimeError(f'exec in {container} failed ({p.returncode}): {p.stdout}')
    return p


def checkpoint(session):
    return json.loads(play.output('exec',play.names(session)[0],'cat','/var/lib/nightwatch/checkpoint.json'))


def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def connect_all(session,dest):
    dest.mkdir(parents=True,exist_ok=True)
    peers={f'p{i}':Peer(session,f'p{i}',dest) for i in range(1,6)}
    for p in peers.values():p.kind('role')
    for p in peers.values():p.wait(lambda m:m.get('kind')=='state' and m['phase']=='night')
    return peers


def resource_test(dest,keep):
    session='nightwatch23-isolation-'+str(time.time_ns());peers={}
    try:
        play.start(session,5,seed=7,durations=(120,3,3))
        peers=connect_all(session,dest)
        identities={u:p.wait(lambda m:'local_uid' in m) for u,p in peers.items()}
        assert len({v['local_uid'] for v in identities.values()})==5
        assert len({v['pid_namespace'] for v in identities.values()})==5
        permissions=json.loads(exec_python(peers['p1'].container,PERMISSIONS).stdout)
        assert all(permissions[k]['blocked'] for k in ['read_only_root','cannot_unlink_broker_socket','cannot_read_moderator_checkpoint'])
        server_ns=exec_python(play.names(session)[0],"import os;print(os.readlink('/proc/self/ns/pid'))").stdout.strip()
        assert server_ns not in {v['pid_namespace'] for v in identities.values()}
        events_before=exec_python(peers['p1'].container,"print(open('/sys/fs/cgroup/memory.events').read())").stdout
        cpu_process=subprocess.Popen(['docker','exec',peers['p1'].container,'python3','-c',CPU],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        responses=0
        while cpu_process.poll() is None:
            assert peers['p2'].status()['phase']=='night';responses+=1;time.sleep(.6)
        cpu_text=cpu_process.communicate()[0]
        if cpu_process.returncode:raise RuntimeError(cpu_text)
        cpu=json.loads(cpu_text)
        assert cpu['cpu_max'].split()==['25000','100000']
        assert cpu['mean_cpu_percent_one_core']<40
        assert responses>=2
        write(dest/'cpu.json',cpu)
        pids=json.loads(exec_python(peers['p1'].container,PIDS).stdout)
        write(dest/'pids.json',pids)
        assert pids['hit_eagain'] and pids['pids_max']=='32'
        memory=exec_python(peers['p1'].container,MEMORY,check=False)
        assert memory.returncode==137, (memory.returncode,memory.stdout)
        # The memory hog should be the OOM victim, but handle a whole client exit too.
        client_running=play.output('inspect','-f','{{.State.Running}}',peers['p1'].container)=='true'
        events_after=exec_python(peers['p1'].container,"print(open('/sys/fs/cgroup/memory.events').read())").stdout if client_running else 'client container exited'
        assert peers['p2'].status()['phase']=='night'
        assert play.output('inspect','-f','{{.State.Running}}',play.names(session)[0])=='true'
        result={'session':session,'identities':identities,'moderator_pid_namespace':server_ns,
                'permissions':permissions,'cpu':cpu,'other_player_status_responses_during_cpu_load':responses,
                'pids':pids,'memory_hog_exit_code':memory.returncode,'memory_events_before':events_before,
                'memory_events_after':events_after,'client_survived_oom':client_running,
                'moderator_and_other_player_responsive_after_oom':True}
        write(dest/'results.json',result)
        print('ISOLATION PASS: CPU, memory OOM, PID limit, read-only root, private state and namespace boundaries.',flush=True)
        return result
    finally:
        for p in peers.values():p.close()
        with (dest/'moderator.txt').open('w') as handle:
            subprocess.run(['docker','logs',play.names(session)[0]],stdout=handle,stderr=subprocess.STDOUT)
        if not keep:play.stop(session,purge=True)
        else:
            subprocess.run(['docker','stop',play.names(session)[0]],stdout=subprocess.DEVNULL)
            print('Retained isolation evidence containers: '+session,flush=True)


def recovery_test(dest,keep):
    session='nightwatch23-recovery-'+str(time.time_ns());peers={}
    try:
        play.start(session,5,seed=7,durations=(15,3,3))
        peers=connect_all(session,dest/'before')
        roles={u:p.kind('role')['role'] for u,p in peers.items()}
        wolf=next(u for u,r in roles.items() if r=='wolf')
        seer=next(u for u,r in roles.items() if r=='seer')
        guard=next(u for u,r in roles.items() if r=='guard')
        victim=next(u for u,r in roles.items() if r=='villager')
        phase_id=peers[wolf].status()['phase_id']
        peers[wolf].command('strike',phase_id,target=victim)
        peers[seer].command('inspect',phase_id,target=wolf)
        peers[guard].command('protect',phase_id,target=seer)
        before=checkpoint(session);write(dest/'checkpoint-before.json',before)
        play.run('kill','--signal','KILL',play.names(session)[0],stdout=subprocess.DEVNULL)
        for p in peers.values():p.close()
        peers={}
        for user in roles:play.run('rm',session+'-'+user,stdout=subprocess.DEVNULL)
        play.resume(session)
        peers=connect_all(session,dest/'after')
        after=checkpoint(session);write(dest/'checkpoint-after.json',after)
        for field in ['roles','alive','choices','phase','phase_id','round','last_protected']:
            assert before[field]==after[field],field
        assert {u:p.kind('role')['role'] for u,p in peers.items()}==roles
        discussion=peers[seer].wait(lambda m:m.get('kind')=='state' and m['phase']=='discussion',timeout=25)
        inspection=peers[seer].kind('inspection')
        assert inspection['target']==wolf and inspection['alignment']=='wolf'
        marker='Private verification message after recovery'
        marks={u:len(p.messages) for u,p in peers.items()}
        peers[seer].send({'cmd':'whisper','phase_id':discussion['phase_id'],'to':[guard],'text':marker})
        for u in [seer,guard]:peers[u].wait(lambda m:m.get('kind')=='chat' and m.get('text')==marker,after=marks[u])
        time.sleep(.1)
        assert all(not any(m.get('text')==marker for m in p.messages[marks[u]:]) for u,p in peers.items() if u not in [seer,guard])
        vote=peers[seer].wait(lambda m:m.get('kind')=='state' and m['phase']=='vote',timeout=8)
        alive=vote['alive']
        assert victim not in alive
        for user in alive:
            if user!=wolf:peers[user].command('vote',vote['phase_id'],target=wolf)
        results={u:p.kind('result',timeout=10) for u,p in peers.items()}
        assert all(r['winner']=='VILLAGE' for r in results.values())
        result={'session':session,'restored_fields':['roles','alive','choices','phase','phase_id','round','last_protected'],
                'saved_actions_recovered':before['choices'],'same_private_roles_after_restart':True,
                'inspection_resolved_after_recovery':inspection,'private_multicast_after_recovery':True,
                'majority_threshold_in_vote':vote['majority_needed'],'living_players_in_vote':len(alive),
                'final_winner':'VILLAGE','all_clients_received_result':True}
        write(dest/'results.json',result)
        print('RECOVERY PASS: SIGKILL, same roles/actions/phase, private delivery, majority vote and completed match.',flush=True)
        return result
    finally:
        for p in peers.values():p.close()
        with (dest/'moderator.txt').open('w') as handle:
            subprocess.run(['docker','logs',play.names(session)[0]],stdout=handle,stderr=subprocess.STDOUT)
        if not keep:play.stop(session,purge=True)
        else:print('Retained recovery evidence containers: '+session,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',default=str(play.ROOT/'evidence'/'verification'))
    parser.add_argument('--keep',action='store_true')
    args=parser.parse_args();dest=Path(args.evidence);dest.mkdir(parents=True,exist_ok=True)
    (dest/'isolation').mkdir(exist_ok=True);(dest/'recovery').mkdir(exist_ok=True)
    results={'isolation':resource_test(dest/'isolation',args.keep),
             'recovery':recovery_test(dest/'recovery',args.keep)}
    write(dest/'summary.json',results)
    print('All reproducible integration checks passed.',flush=True)


if __name__=='__main__':main()
