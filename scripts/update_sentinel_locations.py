import asyncio
import asyncpg

SENTINEL_LOCATIONS = {
    'cam01': {'name': '01 Chiman bhai Bridge', 'lat': 23.0645, 'lon': 72.5852, 'azimuth': 45.0, 'vms': 'Subhash Bridge, Ahmedabad'},
    'cam02': {'name': '02 Janpath', 'lat': 23.0532, 'lon': 72.5712, 'azimuth': 90.0, 'vms': 'Ashram Road, Ahmedabad'},
    'cam03': {'name': '03 O.N.G.C. Office', 'lat': 23.1028, 'lon': 72.5935, 'azimuth': 180.0, 'vms': 'Chandkheda, Ahmedabad'},
    'cam04': {'name': '04 Paldi Circle', 'lat': 23.0125, 'lon': 72.5622, 'azimuth': 270.0, 'vms': 'Paldi, Ahmedabad'},
    'cam05': {'name': '05 Visat teen Rasta', 'lat': 23.0895, 'lon': 72.5873, 'azimuth': 0.0, 'vms': 'Sabarmati, Ahmedabad'},
    'cam06': {'name': '06 Timbavadi gate-Junagadh', 'lat': 21.5034, 'lon': 70.4485, 'azimuth': 120.0, 'vms': 'Junagadh'},
    'cam07': {'name': '07 hero-showroom-gir-somnath', 'lat': 20.9123, 'lon': 70.3621, 'azimuth': 180.0, 'vms': 'Somnath'},
    'cam08': {'name': '08 majewadi-gate-junagadh', 'lat': 21.5285, 'lon': 70.4652, 'azimuth': 60.0, 'vms': 'Junagadh'},
    'cam09': {'name': '09 new-bypass-near-by-circle-junagadh-2', 'lat': 21.5150, 'lon': 70.4320, 'azimuth': 240.0, 'vms': 'Junagadh'},
    'cam10': {'name': '10 char-chowk-road-2-junagadh', 'lat': 21.5210, 'lon': 70.4580, 'azimuth': 300.0, 'vms': 'Junagadh'},
    'cam11': {'name': '11 dolatpara-junagadh', 'lat': 21.5430, 'lon': 70.4720, 'azimuth': 30.0, 'vms': 'Junagadh'},
    'cam12': {'name': '12 Tri Mandir Adalaj Tollnaka', 'lat': 23.1685, 'lon': 72.5824, 'azimuth': 340.0, 'vms': 'Adalaj, Gandhinagar'},
    'cam13': {'name': '13 CN Vidhyalaya', 'lat': 23.0255, 'lon': 72.5480, 'azimuth': 150.0, 'vms': 'Ambawadi, Ahmedabad'},
    'cam14': {'name': '14 Delight RLVD', 'lat': 23.0515, 'lon': 72.5250, 'azimuth': 80.0, 'vms': 'Drive-In, Ahmedabad'},
    'cam15': {'name': '15 Suvidha park', 'lat': 23.0380, 'lon': 72.5310, 'azimuth': 210.0, 'vms': 'Suvidha Park, Ahmedabad'},
    'cam16': {'name': '16 Visat P2', 'lat': 23.0910, 'lon': 72.5890, 'azimuth': 190.0, 'vms': 'Visat, Ahmedabad'},
    'cam17': {'name': '17 Rajkot Bus Port CCTV', 'lat': 22.3039, 'lon': 70.8022, 'azimuth': 90.0, 'vms': 'Rajkot Bus Port'},
    'cam18': {'name': '18 Rajkot CCTV', 'lat': 22.2980, 'lon': 70.7950, 'azimuth': 180.0, 'vms': 'Rajkot Central'},
    'cam19': {'name': '19 KHAPARIA GRAM PANCHAYAT', 'lat': 20.8142, 'lon': 72.9854, 'azimuth': 45.0, 'vms': 'Gandevi, Navsari'},
    'cam20': {'name': '20 Mohanpura', 'lat': 23.8510, 'lon': 72.1280, 'azimuth': 135.0, 'vms': 'Patan'},
    'cam21': {'name': '23 Patan Dethali Char Rasta', 'lat': 23.8340, 'lon': 72.1350, 'azimuth': 225.0, 'vms': 'Patan'},
    'cam22': {'name': '28 BK Mervada tran Rasta', 'lat': 24.1720, 'lon': 72.4350, 'azimuth': 315.0, 'vms': 'Banaskantha'},
    'cam23': {'name': '30 kheram', 'lat': 22.7540, 'lon': 72.6840, 'azimuth': 15.0, 'vms': 'Kheda'},
    'cam24': {'name': '33 dehgam', 'lat': 23.1680, 'lon': 72.8120, 'azimuth': 75.0, 'vms': 'Dehgam, Gandhinagar'},
    'cam25': {'name': '34 dhanori', 'lat': 20.8920, 'lon': 72.9250, 'azimuth': 165.0, 'vms': 'Navsari'},
    'cam26': {'name': '35 TANKAL', 'lat': 20.7850, 'lon': 73.0420, 'azimuth': 255.0, 'vms': 'Navsari'},
    'cam27': {'name': '36 bilimora', 'lat': 20.7630, 'lon': 72.9550, 'azimuth': 345.0, 'vms': 'Bilimora'},
    'cam28': {'name': '37 bilimora', 'lat': 20.7680, 'lon': 72.9610, 'azimuth': 110.0, 'vms': 'Bilimora'},
    'cam29': {'name': '38 bilimora', 'lat': 20.7720, 'lon': 72.9580, 'azimuth': 200.0, 'vms': 'Bilimora'},
    'cam30': {'name': 'Gandhidham Rambaugh p2', 'lat': 23.0750, 'lon': 70.1330, 'azimuth': 290.0, 'vms': 'Gandhidham, Kutch'},
}

async def run():
    conn = await asyncpg.connect('postgresql://trinetra:change-me-local-dev@127.0.0.1:5432/trinetra')
    for cam_id, meta in SENTINEL_LOCATIONS.items():
        code = f"SENTINEL-{cam_id.upper()}"
        query = f"""
            UPDATE cameras
            SET location_geom = ST_SetSRID(ST_MakePoint({meta['lon']}, {meta['lat']}), 4326),
                azimuth_angle = {meta['azimuth']},
                vms_vendor = '{meta['vms']}'
            WHERE global_camera_code = '{code}';
        """
        await conn.execute(query)
    print("Updated all 30 cameras with distinct geographic coordinates!")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(run())
