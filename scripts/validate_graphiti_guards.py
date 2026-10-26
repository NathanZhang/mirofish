"""Live destructive-operation barriers and embedding protocol checks."""
import json
import sys
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.config import Config


def main():
    out=Path(sys.argv[1]);state=json.loads((out/'run.json').read_text())
    client=httpx.Client(trust_env=False,timeout=60)
    results={}
    for name,method,path,body in [
        ('delete_active','DELETE',f"graph/delete/{state['graph_id']}",None),
        ('reset_active','POST',f"graph/project/{state['project_id']}/reset",{}),
        ('rerun_same_graph','POST','simulation/start',{'simulation_id':state['simulation_id'],'force':True,'enable_graph_memory_update':True}),
        ('report_before_memory_barrier','POST','report/generate',{'simulation_id':state['simulation_id']})]:
        response=client.request(method,'http://127.0.0.1:5001/api/'+path,json=body)
        results[name]={'status':response.status_code,'body':response.json()}
        assert response.status_code==409,(name,response.status_code,response.text)
    from dotenv import dotenv_values
    config=dotenv_values(ROOT/'deploy/graphiti-local/.env')
    headers={'Authorization':'Bearer '+config['GRAPH_EMBEDDING_API_KEY']}
    texts=['周先生担忧阅读室噪声','小林负责志愿者排班']
    batch=client.post('http://127.0.0.1:5102/v1/embeddings',headers=headers,json={'model':'BAAI/bge-m3','input':texts})
    batch.raise_for_status();vectors=[row['embedding'] for row in batch.json()['data']]
    assert len(vectors)==2 and all(len(v)==1024 for v in vectors)
    differences=[]
    for text,vector in zip(texts,vectors):
        single=client.post('http://127.0.0.1:5102/v1/embeddings',headers=headers,json={'model':'BAAI/bge-m3','input':text})
        single.raise_for_status();other=single.json()['data'][0]['embedding']
        difference=max(abs(a-b) for a,b in zip(vector,other));differences.append(difference)
        assert difference<1e-5
    wrong=client.post('http://127.0.0.1:5102/v1/embeddings',headers=headers,json={'model':'wrong-model','input':'test'})
    assert wrong.status_code==400
    results['embedding']={'batch_count':2,'dimension':1024,'single_batch_max_differences':differences,'wrong_model_status':wrong.status_code}
    unauth=client.get('http://127.0.0.1:5101/internal/v1/readyz')
    assert unauth.status_code==401
    results['unauthenticated_internal_api']=unauth.status_code
    (out/'live-guards.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
