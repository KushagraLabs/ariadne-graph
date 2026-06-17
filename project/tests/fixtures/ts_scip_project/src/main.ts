import { Dog } from './dog';
import { utilHelper as bar } from './barrel';

export function main(): number {
  const d = new Dog();
  d.sound();
  return bar(5);
}
