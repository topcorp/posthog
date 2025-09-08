# Liquid Templating in PostHog CDP

Liquid is a templating language that allows you to customize your destination payloads with dynamic data from PostHog events and person properties.

## Basic Syntax

### Variables
Use double curly braces to output variables:
```liquid
{{ person.name }}
{{ event.properties.page_title }}
```

### Filters
Apply filters to transform data:
```liquid
{{ person.name | upcase }}
{{ event.timestamp | date: "%Y-%m-%d" }}
{{ event.properties.price | round: 2 }}
```

## Available Data in PostHog

### Event Data
- `{{ event.event }}` - Event name
- `{{ event.timestamp }}` - Event timestamp
- `{{ event.properties }}` - Event properties object
- `{{ event.properties.property_name }}` - Specific event property

### Person Data
- `{{ person.properties }}` - Person properties object
- `{{ person.properties.email }}` - Person email
- `{{ person.properties.name }}` - Person name

### Common Filters
- `{{ value | upcase }}` - Convert to uppercase
- `{{ value | downcase }}` - Convert to lowercase  
- `{{ value | capitalize }}` - Capitalize first letter
- `{{ value | strip }}` - Remove whitespace
- `{{ value | truncate: 50 }}` - Truncate to 50 characters
- `{{ value | default: "fallback" }}` - Use fallback if value is empty

## Control Flow

### Conditionals
```liquid
{% if person.properties.email %}
  "email": "{{ person.properties.email }}"
{% endif %}
```

### Loops
```liquid
{% for property in event.properties %}
  "{{ property[0] }}": "{{ property[1] }}"
{% endfor %}
```

## Best Practices

1. **Always check for data existence** before using it:
   ```liquid
   {% if person.properties.email %}
     {{ person.properties.email }}
   {% else %}
     "unknown@example.com"
   {% endif %}
   ```

2. **Use appropriate filters** for data formatting:
   ```liquid
   {{ event.timestamp | date: "%Y-%m-%d %H:%M:%S" }}
   ```

3. **Handle arrays and objects safely**:
   ```liquid
   {% if event.properties.items.size > 0 %}
     {{ event.properties.items | first }}
   {% endif %}
   ```

## Examples

### Basic Event Payload
```liquid
{
  "event_name": "{{ event.event }}",
  "timestamp": "{{ event.timestamp }}",
  "user_email": "{{ person.properties.email | default: 'unknown' }}"
}
```

### Conditional Properties
```liquid
{
  "event": "{{ event.event }}"
  {% if person.properties.email %},
  "email": "{{ person.properties.email }}"
  {% endif %}
  {% if event.properties.revenue %},
  "revenue": {{ event.properties.revenue }}
  {% endif %}
}
```

## External Reference

For complete Liquid syntax reference, see the [official Liquid documentation](https://liquidjs.com/filters/overview.html).