create schema if not exists animals;
create table if not exists animals.config (
    id integer primary key,
    cat_api_key text not null,
    dog_api_key text not null
);
insert into animals.config (id, cat_api_key, dog_api_key)
    values (0, '', '')
    on conflict (id) do nothing;