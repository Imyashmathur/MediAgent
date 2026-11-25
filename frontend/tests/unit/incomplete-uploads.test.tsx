import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import IncompleteUploads from '../../app/incomplete-uploads';

afterEach(cleanup);

it('lists and resumes interrupted uploads',async()=>{
  let completed=false;
  const api=vi.fn(async(path:string)=>{
    if(path.endsWith('/resume')) {completed=true; return {} as Response;}
    return {json:async()=>completed?[]:[{id:'synthetic',filename:'synthetic.pdf',status:'pending',created_at:0}],headers:new Headers()} as Response;
  });
  const onCompleted=vi.fn(async()=>{});
  render(<IncompleteUploads api={api} onError={vi.fn()} onCompleted={onCompleted}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh incomplete uploads'}));
  await screen.findByText('synthetic.pdf · pending');
  fireEvent.click(screen.getByRole('button',{name:'Resume upload'}));
  await screen.findByText('Upload completed.');
  expect(onCompleted).toHaveBeenCalledOnce();
  expect(api).toHaveBeenCalledWith('documents/incomplete-uploads/synthetic/resume',{method:'POST'});
});

 it('opens older pages of incomplete uploads',async()=>{
  const api=vi.fn(async(path:string)=>{
    const older=path.includes('cursor=');
    return {json:async()=>[{id:older?'older':'newer',filename:older?'older.pdf':'newer.pdf',status:'pending',created_at:0}],headers:new Headers(older?{}:{'x-next-cursor':'synthetic-cursor'})} as Response;
  });
  render(<IncompleteUploads api={api} onError={vi.fn()} onCompleted={vi.fn(async()=>{})}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh incomplete uploads'}));
  await screen.findByText('newer.pdf · pending');
  fireEvent.click(screen.getByRole('button',{name:'Older incomplete uploads'}));
  await screen.findByText('older.pdf · pending');
  expect(api).toHaveBeenLastCalledWith('documents/incomplete-uploads?cursor=synthetic-cursor');
  expect(screen.queryByRole('button',{name:'Older incomplete uploads'})).toBeNull();
 });

it('requests discard and displays ongoing cleanup monitoring',async()=>{
  let discarded=false;
  const api=vi.fn(async(path:string)=>{
    if(path.endsWith('/discard')) {discarded=true; return {} as Response;}
    return {headers:new Headers(),json:async()=>[{id:'synthetic',filename:discarded?'Discarded upload':'synthetic.pdf',status:discarded?'discarding':'pending',cleanup_result:discarded?'absent':null,cleanup_next_check_at:discarded?1000:null,created_at:0}]} as Response;
  });
  render(<IncompleteUploads api={api} onError={vi.fn()} onCompleted={vi.fn(async()=>{})}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh incomplete uploads'}));
  fireEvent.click(await screen.findByRole('button',{name:'Discard upload'}));
  await screen.findByText('Discard requested; object cleanup remains under monitoring.');
  expect(screen.getByText(/Discarded upload · discarding · last cleanup check: absent · next check after/)).toBeTruthy();
  expect(screen.queryByRole('button',{name:'Resume upload'})).toBeNull();
  expect(api).toHaveBeenCalledWith('documents/incomplete-uploads/synthetic/discard',{method:'POST'});
});
