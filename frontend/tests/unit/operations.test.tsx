import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import Operations from '../../app/operations';

afterEach(cleanup);

it('pages older audit events using the submitted filters', async()=>{
  const item=(id:string)=>({id, actor_id:null, patient_id:null, action:`event_${id}`, resource_type:'document', outcome:'success', correlation_id:'synthetic', created_at:1000});
  const api=vi.fn(async (path:string)=>{
    if(path.includes('cursor=')) return {json:async()=>[item('older')],headers:new Headers()} as Response;
    return {json:async()=>[item('newer')],headers:new Headers({'x-next-cursor':'synthetic-cursor'})} as Response;
  });
  render(<Operations api={api} onError={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('Action (optional)'),{target:{value:'synthetic_event'}});
  fireEvent.change(screen.getByLabelText('Outcome'),{target:{value:'success'}});
  fireEvent.click(screen.getByRole('button',{name:'Review audit events'}));
  await screen.findByText('event_newer · success');
  fireEvent.change(screen.getByLabelText('Action (optional)'),{target:{value:'changed'}});
  fireEvent.click(screen.getByRole('button',{name:'Older audit events'}));
  await screen.findByText('event_older · success');
  expect(api).toHaveBeenLastCalledWith('admin/audit?action=synthetic_event&outcome=success&cursor=synthetic-cursor');
  expect(screen.queryByText('event_newer · success')).toBeNull();
  expect(screen.queryByRole('button',{name:'Older audit events'})).toBeNull();
});

it('shows aggregate knowledge job states and possible stale work', async()=>{
  const api=vi.fn(async()=>({json:async()=>({database:'connected',redis:'connected',document_jobs:{},knowledge_import_jobs:{completed:2},knowledge_index_jobs:{failed:1,queued:2},possibly_stale_index_jobs:1,job_dispatch:{},reset_mail:{},document_purges:{},incomplete_object_uploads:{pending:2,stored:1},old_incomplete_object_uploads:1,discarded_object_uploads:{error:1},discard_error_categories:{integrity_conflict:1},overdue_discard_checks:2,oldest_discard_check_lag_seconds:600,possibly_stale_jobs:0,stale_after_seconds:300,api_metrics:{responses:{},latency_buckets:{},provider_failures:{}}})} as Response));
  render(<Operations api={api} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh status'}));
  await screen.findByText('Incomplete object uploads: pending: 2 · stored: 1');
  await screen.findByText('Incomplete object uploads older than 24 hours: 1');
  await screen.findByText('Discarded upload cleanup checks: error: 1');
  await screen.findByText('Discard cleanup errors: object integrity or association conflict: 1');
  await screen.findByText('Discard checks more than 5 minutes overdue: 2 · oldest lag: 600 seconds');
  await screen.findByText('Knowledge import jobs: completed: 2');
  await screen.findByText('Knowledge indexing jobs: failed: 1 · queued: 2');
  await screen.findByText('Knowledge indexing jobs queued or running for over 5 minutes: 1');
});
