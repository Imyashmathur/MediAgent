import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import Consultations from '../../app/consultations';

afterEach(cleanup);

it('opens older consultation pages and refreshes to the newest',async()=>{
  const api=vi.fn(async(path:string)=>{
    const older=path.includes('cursor=');
    return {headers:new Headers(older?{}:{'x-next-cursor':'cursor'}),json:async()=>[{id:older?'older':'newer',title:older?'Older consultation':'Newer consultation',doctor_id:'doctor',occurred_at:1000}]} as Response;
  });
  render(<Consultations api={api} patientId="patient" isDoctor={false} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open consultation history'}));
  await screen.findByText('Newer consultation');
  fireEvent.click(screen.getByRole('button',{name:'Older consultations'}));
  await screen.findByText('Older consultation');
  expect(api).toHaveBeenLastCalledWith('patients/patient/consultations?cursor=cursor');
  fireEvent.click(screen.getByRole('button',{name:'Open consultation history'}));
  await screen.findByText('Newer consultation');
});
