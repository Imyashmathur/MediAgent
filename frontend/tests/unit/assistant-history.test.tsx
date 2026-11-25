import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import Assistant from '../../app/assistant';
const patientId='00000000-0000-4000-8000-000000000001';

afterEach(()=>{cleanup(); vi.unstubAllGlobals();});

it('loads older assistant history and returns to the latest page', async()=>{
  class FakeSocket {onopen:unknown; onmessage:unknown; onclose:unknown; onerror:unknown; close() {} send() {}}
  vi.stubGlobal('WebSocket', FakeSocket);
  const api=vi.fn(async(path:string)=>{
    if(path==='assistant/preferences') return {json:async()=>({enabled:false,response_style:null})} as Response;
    if(path===`conversations?patient_id=${patientId}`) return {json:async()=>[{id:'conversation',patient_id:patientId,created_at:1000}],headers:new Headers()} as Response;
    if(path.endsWith('/ticket')) return {json:async()=>({ticket:'synthetic'})} as Response;
    const older=path.includes('cursor=');
    return {json:async()=>[{id:older?'old':'new',role:'user',content:older?'Older synthetic question':'Newest synthetic question',citations:[],review_status:null}], headers:new Headers(older?{}:{'x-next-cursor':'next'})} as Response;
  });
  render(<Assistant api={api} patientId={patientId} canDraft={false} onError={vi.fn()}/>);
  const select=await screen.findByLabelText('Conversation');
  fireEvent.change(select,{target:{value:'conversation'}});
  await screen.findByText('Newest synthetic question');
  fireEvent.click(screen.getByRole('button',{name:'Older messages'}));
  await screen.findByText('Older synthetic question');
  expect(api).toHaveBeenCalledWith('conversations/conversation/messages?cursor=next');
  fireEvent.click(screen.getByRole('button',{name:'Latest messages'}));
  await screen.findByText('Newest synthetic question');
});

it('sends profile context only when explicitly selected', async()=>{
  const sent:string[]=[];
  let connection:FakeSocket | undefined;
  class FakeSocket {
    onopen:()=>void=()=>{}; onmessage:(event:{data:string})=>void=()=>{}; onclose:()=>void=()=>{}; onerror:()=>void=()=>{};
    constructor() {connection=this;}
    send(message:string) {sent.push(message);}
    close() {}
  }
  vi.stubGlobal('WebSocket', FakeSocket);
  const api=vi.fn(async(path:string)=>{
    if(path==='assistant/preferences') return {json:async()=>({enabled:false,response_style:null})} as Response;
    if(path===`conversations?patient_id=${patientId}`) return {json:async()=>[{id:'conversation',patient_id:patientId,created_at:1000}],headers:new Headers()} as Response;
    if(path.endsWith('/ticket')) return {json:async()=>({ticket:'synthetic'})} as Response;
    return {json:async()=>[],headers:new Headers()} as Response;
  });
  render(<Assistant api={api} patientId={patientId} canDraft={false} onError={vi.fn()}/>);
  fireEvent.change(await screen.findByLabelText('Conversation'),{target:{value:'conversation'}});
  await waitFor(()=>expect(connection).toBeDefined());
  act(()=>connection!.onmessage({data:JSON.stringify({type:'ready'})}));
  fireEvent.change(screen.getByLabelText('Question'),{target:{value:'Synthetic question'}});
  fireEvent.click(screen.getByLabelText('Patient-reported allergies, medications and medical history'));
  fireEvent.click(screen.getByRole('button',{name:'Send'}));
  expect(JSON.parse(sent[0])).toMatchObject({type:'turn',include_profile:true,include_knowledge:true,question:'Synthetic question'});
});

it('loads an older patient-scoped conversation after an empty page', async()=>{
  const api=vi.fn(async(path:string)=>{
    if(path==='assistant/preferences') return {json:async()=>({enabled:false,response_style:null})} as Response;
    if(path===`conversations?patient_id=${patientId}`) return {json:async()=>[],headers:new Headers({'x-next-cursor':'cursor'})} as Response;
    if(path===`conversations?patient_id=${patientId}&cursor=cursor`) return {json:async()=>[{id:'older',patient_id:patientId,created_at:1000}],headers:new Headers()} as Response;
    throw new Error('Unexpected request');
  });
  render(<Assistant api={api} patientId={patientId} canDraft={false} onError={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Load older conversations'}));
  expect(await screen.findByLabelText('Conversation')).toBeTruthy();
  expect(screen.queryByRole('button',{name:'Load older conversations'})).toBeNull();
});

it('labels historical approvals as time-bound and shows reference version',async()=>{
  class FakeSocket {onopen:unknown; onmessage:unknown; onclose:unknown; onerror:unknown; close() {} send() {}}
  vi.stubGlobal('WebSocket', FakeSocket);
  const api=vi.fn(async(path:string)=>{
    if(path==='assistant/preferences') return {json:async()=>({enabled:false,response_style:null})} as Response;
    if(path===`conversations?patient_id=${patientId}`) return {json:async()=>[{id:'conversation',patient_id:patientId,created_at:1000}],headers:new Headers()} as Response;
    if(path.endsWith('/ticket')) return {json:async()=>({ticket:'synthetic'})} as Response;
    return {json:async()=>[{id:'answer',role:'assistant',content:'Synthetic response',review_status:'unreviewed',citations:[{id:'ref',kind:'approved_reference',title:'Synthetic reference',version:'2'},{id:'note',kind:'doctor_approved_note',title:'Synthetic note'}]}],headers:new Headers()} as Response;
  });
  render(<Assistant api={api} patientId={patientId} canDraft={false} onError={vi.fn()}/>);
  fireEvent.change(await screen.findByLabelText('Conversation'),{target:{value:'conversation'}});
  await screen.findByText(/reference approved when this response was generated; verify current status · version 2/);
  expect(screen.getByText(/note approved when this response was generated; verify current status/)).toBeTruthy();
});
