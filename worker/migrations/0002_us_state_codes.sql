-- Re-label United States jobs that were stored as "other" because the city had a state code
-- ("San Francisco, CA") or the country only appeared in the structured locations.
-- The state codes and foreign places are JSON lists so D1's expression depth limit (100) holds.
UPDATE jobs SET country = 'united-states'
WHERE country = 'other'
  AND (
    json_extract(payload, '$.locations[0].country') IN ('United States', 'USA', 'US')
    OR (
      coalesce(json_extract(payload, '$.locations[0].country'), '') = ''
      AND EXISTS (
        SELECT 1 FROM json_each('["AL","AK","AZ","AR","CA","CO","CT","DC","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY","LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY"]') AS state
        WHERE json_extract(jobs.payload, '$.location_raw') GLOB '*, ' || state.value
           OR json_extract(jobs.payload, '$.location_raw') GLOB '*, ' || state.value || '[^A-Za-z]*'
           OR json_extract(jobs.payload, '$.location_raw') GLOB '*,' || state.value
           OR json_extract(jobs.payload, '$.location_raw') GLOB '*,' || state.value || '[^A-Za-z]*'
      )
      AND NOT EXISTS (
        SELECT 1 FROM json_each('["germany","india","united kingdom","france","brazil","nigeria","singapore","canada","mexico","ireland","netherlands","spain","poland","japan","australia","israel"]') AS place
        WHERE lower(coalesce(json_extract(jobs.payload, '$.location_raw'), '')) LIKE '%' || place.value || '%'
      )
    )
  );
