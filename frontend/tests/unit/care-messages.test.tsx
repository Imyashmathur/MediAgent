import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import CareMessages from '../../app/care-messages';

afterEach(cleanup);

it('opens older messages and refreshes back to the newest page', async()=>{
  const api=vi.fn(async(path:string)=>{
    const older=path.includes('cursor=');
    return {json:async()=>[{id:older?'old':'new',sender_id:'patient',content:older?'Older synthetic message':'New synthetic message',created_at:1000}],headers:new Headers(older?{}:{'x-next-cursor':'next'})} as Response;
  });
  render(<CareMessages api={api} relationships={[{id:'rel',patient_id:'patient',doctor_id:'doctor',status:'active'}]} userId="patient" onError={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('Care relationship'),{target:{value:'rel'}});
  fireEvent.click(screen.getByRole('button',{name:'Refresh messages'}));
  await screen.findByText('New synthetic message');
  fireEvent.click(screen.getByRole('button',{name:'Older messages'}));
  await screen.findByText('Older synthetic message');
  expect(api).toHaveBeenLastCalledWith('relationships/rel/messages?cursor=next');
  expect(screen.queryByText('New synthetic message')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Refresh messages'}));
  await screen.findByText('New synthetic message');
});
