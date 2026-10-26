"""Process memory and socket samples for this checkout, excluding credentials."""
import argparse
import json
import time
from pathlib import Path
import psutil

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('--seconds',type=int,default=1800)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+args.seconds
    with args.output.open('a') as stream:
        while time.monotonic()<deadline:
            rows=[]
            for process in psutil.process_iter(['pid','name']):
                try:
                    if not Path(process.cwd()).is_relative_to(ROOT):continue
                    connections = getattr(process,'net_connections',process.connections)
                    sockets=[{'local':list(c.laddr),'remote':list(c.raddr),'status':c.status}
                             for c in connections(kind='inet')]
                    rows.append({'pid':process.pid,'name':process.name(),'rss':process.memory_info().rss,
                                 'threads':process.num_threads(),'sockets':sockets})
                except (psutil.Error,PermissionError,FileNotFoundError):continue
            memory=psutil.virtual_memory();swap=psutil.swap_memory()
            stream.write(json.dumps({'time':time.time(),'available_memory':memory.available,
                'used_memory':memory.used,'swap_used':swap.used,'swap_in':swap.sin,'swap_out':swap.sout,
                'processes':rows})+'\n');stream.flush()
            time.sleep(5)


if __name__=='__main__':main()
