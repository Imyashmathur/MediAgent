import React from 'react';
export const metadata = {title:'MediAgent', description:'Doctor-supervised healthcare workspace for synthetic development data.'};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="en"><body style={{margin:0,fontFamily:'system-ui',background:'#f4f7fb',color:'#12233f'}}>{children}</body></html>}
