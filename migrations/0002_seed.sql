-- Demo business: "CoolBreeze HVAC", Austin TX (fictional). Safe to re-run: every insert is guarded.
-- Phone numbers use the 555-01xx range reserved for fiction. Never put real numbers in seed data.

INSERT INTO services (code, name, price_usd, duration_min, includes) VALUES
 ('ac_diagnostic',   'AC diagnostic visit',       89.00,  90, 'Technician inspection and written quote. Fee is credited if you proceed with the repair.'),
 ('ac_tuneup',       'AC seasonal tune-up',      129.00,  90, 'Coil clean, filter check, refrigerant pressure check, 21-point inspection.'),
 ('ac_repair_small', 'AC minor repair',          249.00, 120, 'Common repairs such as capacitor or contactor, labour and standard parts.'),
 ('furnace_diagnostic','Furnace diagnostic visit', 89.00, 90, 'Technician inspection and written quote.'),
 ('furnace_tuneup',  'Furnace seasonal tune-up', 119.00,  90, 'Burner clean, heat exchanger inspection, safety controls check.'),
 ('duct_inspection', 'Duct inspection',          149.00, 120, 'Airflow test and leak check of accessible ducts.'),
 ('drain_clearing',  'Drain clearing',           175.00,  90, 'Clearing one blocked drain line, camera check not included.'),
 ('water_heater_check','Water heater check',      99.00,  60, 'Flush, anode and pressure-valve check, leak inspection.')
ON CONFLICT (code) DO NOTHING;

INSERT INTO service_area_zips (zip, note) VALUES
 ('78701','Downtown'),('78702','East Austin'),('78703','Tarrytown'),('78704','South Austin'),
 ('78705','University'),('78721','East Austin'),('78722','Cherrywood'),('78723','Windsor Park'),
 ('78724','Colony Park'),('78725','Southeast'),('78726','Northwest Hills'),('78727','North Austin'),
 ('78728','Wells Branch'),('78729','Anderson Mill'),('78730','Steiner Ranch'),('78731','Allandale'),
 ('78741','Riverside'),('78744','Southeast'),('78745','South Austin'),('78746','Westlake'),
 ('78748','South Austin'),('78749','Circle C'),('78750','Northwest'),('78751','Hyde Park'),
 ('78752','North Loop'),('78753','North Austin'),('78756','Brentwood'),('78757','Crestview'),
 ('78758','North Austin'),('78759','Great Hills')
ON CONFLICT (zip) DO NOTHING;

INSERT INTO technicians (name, phone, on_call)
SELECT v.name, v.phone, v.on_call FROM (VALUES
 ('Dana Ortiz',  '+15125550101', true),
 ('Sam Whitaker','+15125550102', false),
 ('Priya Nair',  '+15125550103', false)
) AS v(name, phone, on_call)
WHERE NOT EXISTS (SELECT 1 FROM technicians);

INSERT INTO faq (question, answer, keywords)
SELECT v.q, v.a, v.k FROM (VALUES
 ('What are your hours?', 'Office hours are Monday to Saturday, 8 am to 6 pm. Emergencies are handled 24/7.', 'hours open close saturday sunday weekend'),
 ('Do you work on weekends?', 'Yes, Saturday appointments are available. For emergencies we respond any day, any time.', 'weekend saturday sunday'),
 ('What areas do you serve?', 'We serve Austin and nearby areas. Share your ZIP code and we will confirm.', 'area service zip serve location austin'),
 ('How much is a diagnostic visit?', 'A diagnostic visit is $89 and is credited toward the repair if you go ahead.', 'diagnostic price cost fee visit quote'),
 ('Do you charge for estimates?', 'The diagnostic visit ($89) includes a written quote. The fee is credited if you proceed.', 'estimate quote free charge'),
 ('What payment methods do you accept?', 'Credit and debit cards, and checks. Payment is due when the work is finished.', 'payment pay card cash check'),
 ('Do you offer financing?', 'Financing is available for larger jobs such as system replacement. The office can share the options.', 'financing finance loan installment'),
 ('How soon can someone come?', 'Most diagnostic visits can be booked within 1 to 2 days. I can check the next openings for you.', 'soon when available how fast schedule'),
 ('What is your cancellation policy?', 'You can cancel or reschedule free of charge more than 24 hours before the visit. Later changes need the owner''s approval and may carry a fee.', 'cancel cancellation reschedule fee policy'),
 ('Are your technicians licensed and insured?', 'Yes. All technicians are licensed and the company is fully insured.', 'licensed insured insurance license'),
 ('Do you give warranties?', 'Repairs carry a 90-day labour warranty. Parts follow the manufacturer warranty.', 'warranty guarantee warrant'),
 ('Should I be home during the visit?', 'An adult (18+) should be home to let the technician in and approve the work.', 'home present adult access'),
 ('What if my AC stops working at night?', 'If it is a safety problem (gas smell, flooding, sparks) we escalate immediately. For a warm house, I can book the next morning.', 'night emergency broken stops warm hot'),
 ('Do you service all brands?', 'Yes, we service all major brands of air conditioners and furnaces.', 'brand carrier trane lennox goodman rheem'),
 ('How do I stop text messages?', 'Reply STOP at any time and we will not text you again.', 'stop unsubscribe opt out texts messages')
) AS v(q, a, k)
WHERE NOT EXISTS (SELECT 1 FROM faq);
