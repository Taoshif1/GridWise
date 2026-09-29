"""Simple concurrent load test for the deployed dashboard."""
import argparse
import concurrent.futures
import json
import time
import urllib.request

def hit(url):
    start=time.perf_counter()
    try:
        with urllib.request.urlopen(url,timeout=15) as r:
            r.read(); status=r.status
    except Exception:
        status=599
    return status,(time.perf_counter()-start)*1000

def pctl(values,p):
    values=sorted(values)
    if not values: return 0
    return values[min(len(values)-1,round((len(values)-1)*p))]

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--base',default='http://localhost:8080')
    a.add_argument('--requests',type=int,default=200)
    a.add_argument('--concurrency',type=int,default=20)
    x=a.parse_args(); url=x.base.rstrip('/')+'/api/dashboard'
    start=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=x.concurrency) as pool:
        rows=list(pool.map(lambda _:hit(url),range(x.requests)))
    elapsed=time.perf_counter()-start
    l=[ms for _,ms in rows]; errors=sum(1 for s,_ in rows if s>=400)
    report={'path':'/api/dashboard','requests':x.requests,'concurrency':x.concurrency,'throughput_rps':round(x.requests/elapsed,2),'error_rate':round(errors/x.requests,4),'latency_ms':{'p50':round(pctl(l,.5),2),'p95':round(pctl(l,.95),2),'p99':round(pctl(l,.99),2)}}
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    main()
