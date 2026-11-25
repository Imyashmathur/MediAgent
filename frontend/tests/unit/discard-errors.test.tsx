import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import DiscardErrors from '../../app/discard-errors';

afterEach(cleanup);

it('pages sanitized errors and schedules a retry',async()=>{
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
    if(init?.method==='POST') return {} as Response;
    const older=path.includes('cursor=');
    return {headers:new Headers(older?{}:{'x-next-cursor':'cursor'}),json:async()=>[{id:older?'older-id':'first-id',category:'integrity_conflict',attempts:2,checked_at:1000,next_check_at:2000}]} as Response;
  });
  render(<DiscardErrors api={api} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Review discard errors'}));
  await screen.findByText(/Check ID: first-id/);
  fireEvent.click(screen.getByRole('button',{name:'Older discard errors'}));
  await screen.findByText(/Check ID: older-id/);
  fireEvent.click(screen.getByRole('button',{name:'Retry cleanup check'}));
  await screen.findByText('Cleanup check scheduled. The result remains uncertain until the purger checks it.');
  expect(api).toHaveBeenCalledWith('admin/operations/discard-errors/older-id/retry',{method:'POST'});
});
